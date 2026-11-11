"""The Vault pane («الخزانة»): a header (search, cards/list, +), the Today strip, and the sections of the project
drawn as cheerful cards - one QTreeView with a painted delegate, so hundreds of files cost nothing to show.

Built the first time it is opened. Folder scans, file facts, pictures and duplicate searches run in the worker
thread; layout pages and charter health (they need the main thread) are done one per timer tick. The project folder
is watched only while the pane is visible."""
import datetime
import os
import subprocess
import sys
from collections import defaultdict, deque

from qgis.core import QgsMimeDataUtils, QgsProject, QgsSettings
from qgis.PyQt.QtCore import (QFileSystemWatcher, QModelIndex, QObject, QPointF, QRect, QRectF, QSize,
                              QSortFilterProxyModel, Qt, QTimer, QUrl, pyqtSignal)
from qgis.PyQt.QtGui import (QBrush, QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QIcon, QLinearGradient,
                             QPainter, QPainterPath, QPen, QPixmap, QStandardItem, QStandardItemModel)
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QDockWidget, QFileDialog, QHBoxLayout,
                                 QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QStyle, QStyledItemDelegate,
                                 QToolButton, QTreeView, QVBoxLayout, QWidget)

from .. import icons
from ..i18n import format_number, is_rtl, tr
from . import RECENT_KEY, catalog, scan, thumbs
from .catalog import Entry, search_words, walk
from .kinds import DATA_KINDS, file_kind, icon_name, tint
from .usage import layer_path, layers_of, norm
from .worker import LOW, NORMAL, URGENT, Worker

ENTRY_ROLE = Qt.ItemDataRole.UserRole + 41
URI_MIME = "application/x-vnd.qgis.qgis.uri"


# ---- look ------------------------------------------------------------------------------------------------------
def is_dark(t):
    return QColor(t["base"]).lightness() < 128


def vault_qss(t):
    return f"""
#BiruniVault {{ background: {t['window']}; }}
#BiruniVault QLineEdit#VaultSearch {{ border: 1px solid {t['border']}; border-radius: 9px; padding: 6px 10px;
    background: {t['base']}; color: {t['text']}; }}
#BiruniVault QLineEdit#VaultSearch:focus {{ border-color: {t['accent']}; }}
#BiruniVault QToolButton#VaultTool {{ border: 1px solid transparent; border-radius: 8px; padding: 4px;
    background: transparent; }}
#BiruniVault QToolButton#VaultTool:hover {{ background: {t['hover']}; border-color: {t['border']}; }}
#BiruniVault QToolButton#VaultTool:checked {{ background: {t['accent_soft']}; border-color: {t['accent']}; }}
#BiruniVault QToolButton#VaultAdd {{ border: none; border-radius: 8px; padding: 4px 6px;
    background: {t['accent']}; }}
#BiruniVault QToolButton#VaultAdd:hover {{ background: {t['accent_hover']}; }}
#BiruniVault QToolButton#VaultAdd::menu-indicator {{ image: none; width: 0; }}
#BiruniVault QWidget#VaultToday {{ background: {t['accent_soft']}; border-radius: 10px; }}
#BiruniVault QLabel#VaultTodayTitle {{ color: {t['accent']}; font-weight: 700; }}
#BiruniVault QToolButton#VaultChip {{ border: none; border-radius: 9px; padding: 3px 8px;
    background: {t['card']}; color: {t['text']}; }}
#BiruniVault QToolButton#VaultChip:hover {{ background: {t['card_hover']}; color: {t['accent']}; }}
#BiruniVault QLabel#VaultQuiet {{ color: {t['text_dim']}; }}
#BiruniVault QLabel#VaultStatus {{ color: {t['caption']}; font-size: 8pt; }}
#BiruniVault QTreeView {{ border: none; background: {t['window']}; outline: 0; }}
#BiruniVault QTreeView::item {{ border: none; padding: 0; }}
#BiruniVault QTreeView::item:selected, #BiruniVault QTreeView::item:hover {{ background: transparent; }}
#BiruniVault QTreeView::branch {{ background: transparent; image: none; border-image: none; }}
"""


def _mix(a, b, k):
    a, b = QColor(a), QColor(b)
    return QColor(round(a.red() * (1 - k) + b.red() * k), round(a.green() * (1 - k) + b.green() * k),
                  round(a.blue() * (1 - k) + b.blue() * k))


def badge_colors(level, t):
    dark = is_dark(t)
    if level == "bad":
        return (QColor("#4C1D1D") if dark else QColor("#FDE4E2")), QColor("#FF9F9A" if dark else "#B42318")
    if level == "warn":
        return (QColor("#45330F") if dark else QColor("#FEF1C7")), QColor("#FCD34D" if dark else "#8A4B07")
    if level in ("good", "sealed"):
        return (QColor("#113A28") if dark else QColor("#DCF7E6")), QColor("#6EE7B7" if dark else "#11673A")
    return QColor(t["accent_soft"]), QColor(t["accent"])


def human_size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def when_text(t):
    if not t:
        return ""
    try:
        d = datetime.datetime.fromtimestamp(t) if not isinstance(t, str) else datetime.datetime.fromisoformat(t)
    except (TypeError, ValueError, OSError):
        return ""
    if d.date() == datetime.date.today():
        return tr("today {time}").format(time=d.strftime("%H:%M"))
    return d.strftime("%Y-%m-%d")


def _n(n):
    return format_number(n, 0)


def count_text(kind, n):
    """"1 layer" / "5 layers" … (one tr() literal per form, for the translators)."""
    one = n == 1
    if kind == "layers":
        return tr("1 layer") if one else tr("{n} layers").format(n=_n(n))
    if kind == "rows":
        return tr("1 row") if one else tr("{n} rows").format(n=_n(n))
    if kind == "features":
        return tr("1 feature") if one else tr("{n} features").format(n=_n(n))
    if kind == "pages":
        return tr("1 page") if one else tr("{n} pages").format(n=_n(n))
    if kind == "fields":
        return tr("1 field") if one else tr("{n} fields").format(n=_n(n))
    if kind == "rules":
        return tr("1 rule") if one else tr("{n} rules").format(n=_n(n))
    if kind == "photos":
        return tr("1 photo") if one else tr("{n} photos").format(n=_n(n))
    if kind == "steps":
        return tr("1 step") if one else tr("{n} steps").format(n=_n(n))
    if kind == "files":
        return tr("1 file") if one else tr("{n} files").format(n=_n(n))
    return _n(n)


def facts_line(e):
    """The one-line summary of an item: count · CRS · size · date."""
    f = e.facts
    bits = []
    layers = f.get("layers") or []
    k = e.kind
    if f.get("missing"):
        return e.path or e.uri
    if k in ("vector", "cad", "table", "layer", "database", "indoor") or e.data.get("db"):
        count = f.get("count")
        if count is None and len(layers) == 1:
            count = layers[0].get("count")
        if len(layers) > 1 or (e.data.get("db") and layers):
            bits.append(count_text("layers", len(layers) + len(f.get("rasters", []))))
        elif count is not None:
            table = f.get("table_only") or (len(layers) == 1 and not layers[0].get("has_geometry", True))
            bits.append(count_text("rows" if table else "features", count))
    if f.get("width"):
        bits.append(tr("{w}×{h} px").format(w=f["width"], h=f["height"]))
    crs = f.get("crs")
    if crs is None and len(layers) == 1:
        crs = layers[0].get("crs")
    if crs and k not in ("database",) and not e.data.get("db"):
        bits.append(crs)
    for key in ("layer_count", "group_count"):
        if f.get(key) is not None:
            bits.append(count_text("layers", f[key]))
    if k in ("layout", "mapbook") and f.get("pages") is not None:
        bits.append(count_text("pages", f["pages"]))
        if f.get("page_size"):
            bits.append("{0} × {1} mm".format(*f["page_size"]))
    if k == "stop":
        if f.get("scale"):
            bits.append("1:" + _n(f["scale"]))
        if f.get("created"):
            bits.append(when_text(f["created"]))
    if k == "charter":
        bits.append(count_text("fields", f.get("charter_fields", 0)))
        if f.get("rules"):
            bits.append(count_text("rules", f["rules"]))
    if k == "photos":
        bits.append(count_text("photos", f.get("count", 0)))
        if f.get("last"):
            bits.append(tr("last {date}").format(date=when_text(f["last"])))
    if k == "figure":
        if f.get("chapter"):
            bits.append(tr("Chapter {n}").format(n=f["chapter"]))
        if f.get("steps"):
            bits.append(count_text("steps", f["steps"]))
    if f.get("files") is not None:
        bits.append(count_text("files", f["files"]))
    if f.get("size") is not None and k not in ("photos", "folder"):
        bits.append(human_size(f["size"]))
    if f.get("mtime") and k not in ("photos",):
        bits.append(when_text(f["mtime"]))
    bits = [b for b in bits if b]
    if is_rtl():  # "297 × 210 mm", "EPSG:32638", "1.7 KB" keep their own order inside an Arabic line
        bits = [b if any("\u0600" <= ch <= "\u06ff" for ch in b) else "\u200e" + b + "\u200e" for b in bits]
    return " · ".join(bits)


# ---- the model ------------------------------------------------------------------------------------------------
class VaultModel(QStandardItemModel):
    """Items are dragged to the map and the Layers panel as the Browser's layers are."""

    def mimeTypes(self):  # noqa: N802
        return [URI_MIME]

    def mimeData(self, indexes):  # noqa: N802
        uris = []
        for ix in indexes:
            e = ix.data(ENTRY_ROLE)
            u = entry_uri(e)
            if u is not None:
                uris.append(u)
        return QgsMimeDataUtils.encodeUriList(uris) if uris else None


def entry_uri(e):
    if e is None or not e.droppable():
        return None
    u = QgsMimeDataUtils.Uri()
    u.layerType = e.layer_type
    u.providerKey = e.provider or ("gdal" if e.layer_type == "raster" else "ogr")
    u.name = e.title
    uri = e.uri or e.path
    if e.table and "|layername=" not in uri and u.providerKey == "ogr":
        uri = f"{uri}|layername={e.table}"
    u.uri = uri
    return u


class VaultFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.words = []
        self.setRecursiveFilteringEnabled(True)

    def set_words(self, words):
        self.words = words
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):  # noqa: N802
        if not self.words:
            return True
        ix = self.sourceModel().index(row, 0, parent)
        e = ix.data(ENTRY_ROLE)
        if e is None or e.row != "item":
            return False
        return e.matches(self.words)


# ---- painting -------------------------------------------------------------------------------------------------
class VaultDelegate(QStyledItemDelegate):
    button_clicked = pyqtSignal(object)

    CARD_H, LIST_H, SECTION_H, GROUP_H, EMPTY_H, STRIP_H = 74, 32, 54, 34, 70, 112

    def __init__(self, dock):
        super().__init__(dock)
        self.dock = dock
        self.mode = "cards"

    @property
    def t(self):
        return self.dock.tokens()

    def sizeHint(self, option, index):  # noqa: N802
        e = index.data(ENTRY_ROLE)
        w = max(200, option.rect.width())
        if e is None:
            return QSize(w, 24)
        if e.row == "section":
            return QSize(w, self.SECTION_H)
        if e.row == "group":
            return QSize(w, self.GROUP_H)
        if e.row == "empty":
            return QSize(w, self.EMPTY_H)
        if e.row == "loading":
            return QSize(w, 28)
        if self.mode == "list":
            return QSize(w, self.LIST_H)
        if e.kind == "photos" and e.data.get("strip_paths"):
            return QSize(w, self.STRIP_H)
        return QSize(w, self.CARD_H)

    # -- helpers ---------------------------------------------------------------------------------------------
    @staticmethod
    def _vr(option, r):
        return QStyle.visualRect(option.direction, option.rect, r)

    def _text(self, p, option, rect, text, color, size_delta=0.0, bold=False, weight=None, align=None):
        f = QFont(option.font)
        if size_delta:
            f.setPointSizeF(max(6.0, f.pointSizeF() + size_delta))
        if bold:
            f.setBold(True)
        if weight is not None:
            f.setWeight(weight)
        p.setFont(f)
        p.setPen(QColor(color))
        vr = self._vr(option, rect)
        elided = QFontMetrics(f).elidedText(text or "", Qt.TextElideMode.ElideRight, vr.width())
        flags = (align if align is not None else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter
        p.drawText(vr, int(flags), elided)
        return QFontMetrics(f).horizontalAdvance(elided)

    def _icon(self, p, option, rect, name, color):
        vr = self._vr(option, rect)
        pm = icons.icon(name if icons.exists(name) else "file", QColor(color).name()).pixmap(vr.size())
        p.drawPixmap(vr, pm)

    def _chevron(self, p, option, rect, open_, color):
        vr = self._vr(option, rect)
        c = QPointF(vr.center())
        s = 3.6
        rtl = option.direction == Qt.LayoutDirection.RightToLeft
        path = QPainterPath()
        if open_:
            path.moveTo(c.x() - s, c.y() - s / 2)
            path.lineTo(c.x(), c.y() + s / 2)
            path.lineTo(c.x() + s, c.y() - s / 2)
        else:
            d = -1 if rtl else 1
            path.moveTo(c.x() - d * s / 2, c.y() - s)
            path.lineTo(c.x() + d * s / 2, c.y())
            path.lineTo(c.x() - d * s / 2, c.y() + s)
        p.setPen(QPen(QColor(color), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    @staticmethod
    def pill_width(option, text, size_delta=-1.5, tick=False):
        f = QFont(option.font)
        f.setPointSizeF(max(6.0, f.pointSizeF() + size_delta))
        f.setBold(True)
        return QFontMetrics(f).horizontalAdvance(text) + 14 + (11 if tick else 0)

    def _pill(self, p, option, x, y, text, bg, fg, h=17, size_delta=-1.5, start=True, tick=False):
        """Draws a pill whose start (LTR left / RTL right) is at x (logical); tick: a drawn check mark before the
        text (a font may not have "✓"). Returns its width."""
        f = QFont(option.font)
        f.setPointSizeF(max(6.0, f.pointSizeF() + size_delta))
        f.setBold(True)
        w = self.pill_width(option, text, size_delta, tick)
        r = QRect(x if start else x - w, y, w, h)
        vr = self._vr(option, r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(QRectF(vr), h / 2, h / 2)
        text_r = QRect(r)
        if tick:
            tr_ = self._vr(option, QRect(r.x() + 7, y, 9, h))
            c = QPointF(tr_.center())
            path = QPainterPath()
            path.moveTo(c.x() - 3.5, c.y())
            path.lineTo(c.x() - 1.0, c.y() + 2.6)
            path.lineTo(c.x() + 3.8, c.y() - 2.8)
            p.setPen(QPen(fg, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            text_r = QRect(r.x() + 11, y, w - 11, h)
        p.setFont(f)
        p.setPen(fg)
        p.drawText(self._vr(option, text_r), int(Qt.AlignmentFlag.AlignCenter), text)
        return w

    def _tile(self, p, option, rect, e, radius=10, icon_size=None, solid=False):
        """The kind's coloured square (with its icon), or the item's picture."""
        t = self.t
        dark = is_dark(t)
        col = QColor(tint(e.kind if e.row == "item" or e.row == "group" else e.kind, dark))
        vr = self._vr(option, rect)
        path = QPainterPath()
        path.addRoundedRect(QRectF(vr), radius, radius)
        if e.thumb is not None and e.row == "item":
            back = QColor(t["base"]) if e.kind not in ("layout", "figure") else QColor(t["midlight"])
            soft = QColor(col)
            soft.setAlpha(26 if not dark else 40)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(back)
            p.drawPath(path)
            p.setBrush(soft)
            p.drawPath(path)
            p.save()
            p.setClipPath(path)
            pm = e.thumb
            cover = e.kind in ("stop", "photos", "raster")
            mode = Qt.AspectRatioMode.KeepAspectRatioByExpanding if cover else Qt.AspectRatioMode.KeepAspectRatio
            ratio = p.device().devicePixelRatioF() if p.device() is not None else 1.0
            pad = 0 if cover else 4
            target = QSize(round((vr.width() - 2 * pad) * ratio), round((vr.height() - 2 * pad) * ratio))
            s = pm.scaled(target, mode, Qt.TransformationMode.SmoothTransformation)
            s.setDevicePixelRatio(ratio)
            sw, sh = s.width() / ratio, s.height() / ratio
            p.drawPixmap(QPointF(vr.x() + (vr.width() - sw) / 2, vr.y() + (vr.height() - sh) / 2), s)
            p.restore()
            p.setPen(QPen(QColor(t["border"]), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
            return
        if solid:
            g = QLinearGradient(QPointF(vr.topLeft()), QPointF(vr.bottomRight()))
            g.setColorAt(0, _mix(col, "#FFFFFF", 0.18))
            g.setColorAt(1, col)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(g))
            p.drawPath(path)
            fg = "#FFFFFF" if not dark else "#0B1F1D"
        else:
            soft = QColor(col)
            soft.setAlpha(34 if not dark else 52)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(soft)
            p.drawPath(path)
            fg = col.name()
        isz = icon_size or round(rect.width() * 0.5)
        ir = QRect(rect.x() + (rect.width() - isz) // 2, rect.y() + (rect.height() - isz) // 2, isz, isz)
        self._icon(p, option, ir, icon_name(e.kind), fg)

    # -- paint -----------------------------------------------------------------------------------------------
    def paint(self, p, option, index):
        e = index.data(ENTRY_ROLE)
        if e is None:
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setLayoutDirection(option.direction)
        view = self.dock.tree
        expanded = view.isExpanded(index)
        has_kids = index.model().hasChildren(index)
        try:
            if e.row == "section":
                self._paint_section(p, option, e, expanded)
            elif e.row == "group":
                self._paint_group(p, option, e, expanded)
            elif e.row == "empty":
                self._paint_empty(p, option, e)
            elif e.row == "loading":
                r = option.rect.adjusted(14, 0, -8, 0)
                self._text(p, option, QRect(r.x(), r.y(), r.width(), r.height()),
                           tr("Reading…"), self.t["text_dim"], -0.5)
            elif self.mode == "list":
                self._paint_row(p, option, e, has_kids, expanded)
            else:
                self._paint_card(p, option, e, has_kids, expanded)
        finally:
            p.restore()

    def _paint_section(self, p, option, e, expanded):
        t = self.t
        dark = is_dark(t)
        o = option.rect
        r = QRect(o.x() + 4, o.y() + 8, o.width() - 10, o.height() - 10)
        vr = self._vr(option, r)
        col = QColor(tint(e.kind, dark))
        g = QLinearGradient(QPointF(vr.topLeft()), QPointF(vr.topRight()))
        a = QColor(col)
        a.setAlpha(46 if dark else 30)
        b = QColor(col)
        b.setAlpha(0)
        rtl = option.direction == Qt.LayoutDirection.RightToLeft
        g.setColorAt(0, b if rtl else a)
        g.setColorAt(1, a if rtl else b)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawRoundedRect(QRectF(vr), 12, 12)
        tile = QRect(r.x() + 7, r.y() + (r.height() - 30) // 2, 30, 30)
        self._tile(p, option, tile, e, radius=9, icon_size=18, solid=True)
        x = tile.right() + 11
        end = r.right() - 30
        pill_w = 0
        if e.count is not None:
            bg = QColor(col)
            bg.setAlpha(60 if dark else 36)
            pill_w = self._pill(p, option, end, r.y() + (r.height() - 18) // 2, format_number(e.count, 0), bg,
                                QColor(t["text"]), h=18, size_delta=-1.0, start=False)
        tw = end - pill_w - 8 - x
        if e.subtitle:
            self._text(p, option, QRect(x, r.y() + 3, tw, 20), e.title, t["text"], 1.5, weight=QFont.Weight.Bold)
            self._text(p, option, QRect(x, r.y() + 22, tw, 16), e.subtitle, t["text_dim"], -1.0)
        else:
            self._text(p, option, QRect(x, r.y(), tw, r.height()), e.title, t["text"], 1.5,
                       weight=QFont.Weight.Bold)
        self._chevron(p, option, QRect(r.right() - 24, r.y(), 18, r.height()), expanded, t["text_dim"])

    def _paint_group(self, p, option, e, expanded):
        t = self.t
        o = option.rect
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        r = QRect(o.x() + 4, o.y() + 3, o.width() - 10, o.height() - 4)
        if hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t["hover"]))
            p.drawRoundedRect(QRectF(self._vr(option, r)), 8, 8)
        tile = QRect(r.x() + 4, r.y() + (r.height() - 22) // 2, 22, 22)
        self._tile(p, option, tile, e, radius=7, icon_size=14)
        x = tile.right() + 9
        end = r.right() - 26
        pill_w = 0
        if e.count is not None:
            pill_w = self._pill(p, option, end, r.y() + (r.height() - 16) // 2, format_number(e.count, 0),
                                QColor(t["midlight"]), QColor(t["text_dim"]), h=16, start=False)
        self._text(p, option, QRect(x, r.y(), end - pill_w - 6 - x, r.height()), e.title, t["text"], 0.3,
                   weight=QFont.Weight.DemiBold)
        self._chevron(p, option, QRect(r.right() - 20, r.y(), 14, r.height()), expanded, t["text_dim"])

    def _card_rect(self, option):
        o = option.rect
        return QRect(o.x() + 4, o.y() + 3, o.width() - 10, o.height() - 6)

    def _paint_card(self, p, option, e, has_kids, expanded):
        t = self.t
        dark = is_dark(t)
        r = self._card_rect(option)
        vr = self._vr(option, r)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        sel = bool(option.state & QStyle.StateFlag.State_Selected)
        bad = any(lvl == "bad" for _t, lvl in e.badges)
        p.setBrush(QColor(t["card_hover"] if hover else t["card"]))
        p.setPen(QPen(QColor(t["accent"]) if sel else QColor(_mix(t["border"], t["accent"], 0.35) if hover
                                                              else t["border"]), 1.6 if sel else 1))
        p.drawRoundedRect(QRectF(vr).adjusted(0.5, 0.5, -0.5, -0.5), 11, 11)
        # the kind's colour along the starting edge
        stripe = QRect(r.x() + 1, r.y() + 12, 3, r.height() - 24)
        sv = self._vr(option, stripe)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#E5484D") if bad else QColor(tint(e.kind, dark)))
        p.drawRoundedRect(QRectF(sv), 1.5, 1.5)
        side = 54
        top = r.y() + 9 if e.kind == "photos" and e.data.get("strip_paths") else r.y() + (r.height() - side) // 2
        tile = QRect(r.x() + 10, top, side, side)
        self._tile(p, option, tile, e, radius=9, icon_size=24)
        x = tile.right() + 11
        end = r.right() - (26 if has_kids else 10)
        w = end - x
        y = r.y() + 8
        self._text(p, option, QRect(x, y, w, 19), e.title, t["text"], 0.6, weight=QFont.Weight.DemiBold)
        line = facts_line(e) or e.subtitle
        self._text(p, option, QRect(x, y + 19, w, 16), line, t["text_dim"], -0.8)
        by = y + 38
        if e.kind == "photos" and e.data.get("strip_paths"):
            bx = x
            for pm in e.strip[:6]:
                cell = QRect(bx, by, 34, 34)
                if bx + 34 > end:
                    break
                cv = self._vr(option, cell)
                path = QPainterPath()
                path.addRoundedRect(QRectF(cv), 6, 6)
                p.save()
                p.setClipPath(path)
                s = pm.scaled(cv.size() * 2, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                              Qt.TransformationMode.SmoothTransformation)
                p.drawPixmap(cv, s, QRect((s.width() - cv.width() * 2) // 2, (s.height() - cv.height() * 2) // 2,
                                          cv.width() * 2, cv.height() * 2))
                p.restore()
                bx += 38
            by += 40
        if e.badges:
            bx = x
            for text, level in e.badges[:4]:
                bg, fg = badge_colors(level, t)
                tick = level == "sealed"
                if bx + self.pill_width(option, text, tick=tick) > end:
                    break
                bx += self._pill(p, option, bx, by, text, bg, fg, tick=tick) + 5
        elif e.subtitle and line != e.subtitle:
            self._text(p, option, QRect(x, by - 1, w, 17), e.subtitle, t["caption"], -1.2)
        if has_kids:
            self._chevron(p, option, QRect(r.right() - 22, r.y(), 16, r.height()), expanded, t["text_dim"])

    def _paint_row(self, p, option, e, has_kids, expanded):
        t = self.t
        dark = is_dark(t)
        o = option.rect
        r = QRect(o.x() + 4, o.y() + 1, o.width() - 10, o.height() - 2)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        sel = bool(option.state & QStyle.StateFlag.State_Selected)
        if hover or sel:
            p.setPen(QPen(QColor(t["accent"]), 1) if sel else Qt.PenStyle.NoPen)
            p.setBrush(QColor(t["accent_soft"] if sel else t["hover"]))
            p.drawRoundedRect(QRectF(self._vr(option, r)), 7, 7)
        tile = QRect(r.x() + 6, r.y() + (r.height() - 22) // 2, 22, 22)
        self._icon(p, option, tile.adjusted(2, 2, -2, -2), icon_name(e.kind), tint(e.kind, dark))
        x = tile.right() + 8
        end = r.right() - (24 if has_kids else 8)
        if e.badges:
            worst = "bad" if any(b[1] == "bad" for b in e.badges) else (
                "warn" if any(b[1] == "warn" for b in e.badges) else e.badges[0][1])
            _bg, fg = badge_colors(worst, t)
            dot = QRect(end - 8, r.y() + (r.height() - 8) // 2, 8, 8)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fg)
            p.drawEllipse(QRectF(self._vr(option, dot)))
            end -= 14
        short = facts_line(e).split(" · ")[0] if facts_line(e) else ""
        fm = QFontMetrics(option.font)
        sw = min(fm.horizontalAdvance(short) + 6, (end - x) // 2) if short else 0
        name_w = self._text(p, option, QRect(x, r.y(), end - x - sw - 6, r.height()), e.title, t["text"], 0)
        del name_w
        if short:
            self._text(p, option, QRect(end - sw, r.y(), sw, r.height()), short, t["text_dim"], -1.0,
                       align=Qt.AlignmentFlag.AlignRight)
        if has_kids:
            self._chevron(p, option, QRect(r.right() - 20, r.y(), 14, r.height()), expanded, t["text_dim"])

    def button_rect(self, option, e):
        """The button of an empty state (logical rectangle) or None."""
        if not e.data.get("button"):
            return None
        o = option.rect
        r = QRect(o.x() + 4, o.y() + 4, o.width() - 10, o.height() - 8)
        f = QFont(option.font)
        f.setBold(True)
        w = QFontMetrics(f).horizontalAdvance(e.data["button"]) + 24
        return QRect(r.right() - 10 - w, r.y() + (r.height() - 26) // 2, w, 26)

    def _paint_empty(self, p, option, e):
        t = self.t
        o = option.rect
        r = QRect(o.x() + 4, o.y() + 4, o.width() - 10, o.height() - 8)
        vr = self._vr(option, r)
        pen = QPen(QColor(t["mid"] if not is_dark(t) else t["light"]), 1.2, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(QColor(t["base"]))
        p.drawRoundedRect(QRectF(vr).adjusted(0.5, 0.5, -0.5, -0.5), 11, 11)
        tile = QRect(r.x() + 10, r.y() + (r.height() - 34) // 2, 34, 34)
        self._tile(p, option, tile, e, radius=17, icon_size=18)
        br = self.button_rect(option, e)
        x = tile.right() + 10
        end = (br.x() - 8) if br is not None else r.right() - 10
        f = QFont(option.font)
        f.setPointSizeF(f.pointSizeF() - 0.6)
        p.setFont(f)
        p.setPen(QColor(t["text_dim"]))
        tr_ = self._vr(option, QRect(x, r.y() + 4, end - x, r.height() - 8))
        p.drawText(tr_, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap),
                   e.title)
        if br is not None:
            hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
            bv = self._vr(option, br)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t["accent_hover"] if hover else t["accent"]))
            p.drawRoundedRect(QRectF(bv), 13, 13)
            fb = QFont(option.font)
            fb.setBold(True)
            fb.setPointSizeF(fb.pointSizeF() - 0.5)
            p.setFont(fb)
            p.setPen(QColor(t["accent_text"]))
            p.drawText(bv, int(Qt.AlignmentFlag.AlignCenter), e.data["button"])

    def editorEvent(self, event, model, option, index):  # noqa: N802
        from qgis.PyQt.QtCore import QEvent

        e = index.data(ENTRY_ROLE)
        if e is not None and e.row == "empty" and event.type() == QEvent.Type.MouseButtonRelease:
            br = self.button_rect(option, e)
            if br is not None and self._vr(option, br).contains(event.position().toPoint()):
                self.button_clicked.emit(e)
                return True
        return super().editorEvent(event, model, option, index)


# ---- the Today strip ------------------------------------------------------------------------------------------
class TodayStrip(QWidget):
    chip_clicked = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("VaultToday")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.h = QHBoxLayout(self)
        self.h.setContentsMargins(10, 6, 8, 6)
        self.h.setSpacing(6)
        self.chips = []
        self.items = []

    def set_items(self, items, tokens):
        """items: [(target key, icon, text)]."""
        self.items = list(items)
        while self.h.count():
            w = self.h.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        self.chips = []
        title = QLabel(tr("Today"))
        title.setObjectName("VaultTodayTitle")
        self.h.addWidget(title)
        if not items:
            quiet = QLabel(tr("Nothing new yet - a calm day."))
            quiet.setObjectName("VaultQuiet")
            self.h.addWidget(quiet, 1)
            return
        for key, ic, text in items:
            b = QToolButton()
            b.setObjectName("VaultChip")
            b.setText(text)
            b.setIcon(icons.icon(ic, tokens["accent"]))
            b.setIconSize(QSize(14, 14))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.chip_clicked.emit(k))
            self.h.addWidget(b)
            self.chips.append(b)
        self.h.addStretch(1)


# ---- the pane -------------------------------------------------------------------------------------------------
class VaultDock(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("Vault"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setObjectName("BiruniVault")
        self.worker = Worker(self)
        self.worker.done.connect(self._job_done)
        self.cache = thumbs.ThumbCache()
        self._files = None                 # records of the project folder (None: not read yet)
        self._folder = None
        self._photo_facts = None
        self._facts = {}                   # "path|table" -> facts read by the worker
        self._pix = {}                     # picture key -> QPixmap
        self._scores = {}                  # layer id -> charter health
        self._expanded = None              # keys of the open rows (None: the first look)
        self._by_fact = defaultdict(list)  # "path|table" -> entries
        self._item_of = {}                 # id(entry) -> QStandardItem
        self._requested = set()
        self._main = deque()               # (fn, args) done one per tick on the main thread
        self._dirty = True
        self._built_ms = None
        self.entries = []
        self.stats = {"rebuilds": 0, "scans": 0}
        self._jobs_cb = {}
        self._dialogs = []
        self._lazy = {}
        self._known = set()
        self._quiet = False                # keys seen once (their first look was decided)
        self._used_map = None
        self._stops = []
        self._stops_store = None
        self.delegate = None
        self._build()
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(lambda *_: self._later_rescan.start())
        self._later_rescan = QTimer(self, singleShot=True, interval=900, timeout=self.rescan)
        self._later_rebuild = QTimer(self, singleShot=True, interval=350, timeout=self.rebuild)
        self._later_search = QTimer(self, singleShot=True, interval=160, timeout=self._apply_search)
        self._tick = QTimer(self, interval=15, timeout=self._main_tick)
        self.visibilityChanged.connect(self._visible)
        self._connect_project()

    # -- building ------------------------------------------------------------------------------------------------
    def tokens(self):
        return self.plugin.theme.tokens()

    def _build(self):
        body = QWidget()
        body.setObjectName("BiruniVault")
        v = QVBoxLayout(body)
        v.setContentsMargins(8, 8, 8, 4)
        v.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(4)
        self.search = QLineEdit()
        self.search.setObjectName("VaultSearch")
        self.search.setPlaceholderText(tr("Search names, fields, notes…"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda *_: self._later_search.start())
        head.addWidget(self.search, 1)
        self.btn_cards = self._tool("layout-grid", tr("Cards"))
        self.btn_list = self._tool("layout-list", tr("List"))
        for b in (self.btn_cards, self.btn_list):
            b.setCheckable(True)
            head.addWidget(b)
        self.btn_cards.setChecked(True)
        self.btn_cards.clicked.connect(lambda: self.set_mode("cards"))
        self.btn_list.clicked.connect(lambda: self.set_mode("list"))
        self.btn_refresh = self._tool("refresh", tr("Read everything again"))
        self.btn_refresh.clicked.connect(self.refresh)
        head.addWidget(self.btn_refresh)
        self.btn_add = QToolButton()
        self.btn_add.setObjectName("VaultAdd")
        self.btn_add.setToolTip(tr("Add a folder, a database, a new GeoPackage or a project folder"))
        self.btn_add.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_menu = QMenu(self.btn_add)
        self.add_menu.aboutToShow.connect(self._fill_add_menu)
        self.btn_add.setMenu(self.add_menu)
        self.btn_add.setIconSize(QSize(18, 18))
        head.addWidget(self.btn_add)
        v.addLayout(head)
        self.today = TodayStrip()
        self.today.chip_clicked.connect(self.reveal)
        v.addWidget(self.today)
        self.model = VaultModel(self)
        self.proxy = VaultFilter(self)
        self.proxy.setSourceModel(self.model)
        self.tree = QTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(14)
        self.tree.setAnimated(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setMouseTracking(True)
        self.tree.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tree.verticalScrollBar().setSingleStep(18)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.delegate = VaultDelegate(self)
        self.delegate.button_clicked.connect(lambda e: self.run_action(e.data.get("action")))
        self.tree.setItemDelegate(self.delegate)
        self.tree.clicked.connect(self._clicked)
        self.tree.activated.connect(self._activated)
        self.tree.expanded.connect(self._on_expanded)
        self.tree.collapsed.connect(self._on_collapsed)
        v.addWidget(self.tree, 1)
        self.status = QLabel("")
        self.status.setObjectName("VaultStatus")
        v.addWidget(self.status)
        self.setWidget(body)
        self.setMinimumWidth(300)
        self.refresh_theme()

    def _tool(self, icon, tip):
        b = QToolButton()
        b.setObjectName("VaultTool")
        b.setToolTip(tip)
        b.setIconSize(QSize(18, 18))
        b.setProperty("vault_icon", icon)
        b.setAutoRaise(True)
        return b

    def refresh_theme(self):
        t = self.tokens()
        self.widget().setStyleSheet(vault_qss(t))
        for b in (self.btn_cards, self.btn_list, self.btn_refresh):
            b.setIcon(icons.icon(b.property("vault_icon"), t["icon"]))
        self.btn_add.setIcon(icons.icon("square-rounded-plus", t["accent_text"]))
        if getattr(self, "_search_icon", None) is None:
            self._search_icon = self.search.addAction(icons.icon("search", t["placeholder"]),
                                                      QLineEdit.ActionPosition.LeadingPosition)
        else:
            self._search_icon.setIcon(icons.icon("search", t["placeholder"]))
        self.today.set_items(self.today.items, t)
        self.tree.viewport().update()

    def set_mode(self, mode):
        self.delegate.mode = mode
        self.btn_cards.setChecked(mode == "cards")
        self.btn_list.setChecked(mode == "list")
        QgsSettings().setValue("biruni/vault/mode", mode)
        self.tree.doItemsLayout()
        self.tree.viewport().update()

    # -- life ----------------------------------------------------------------------------------------------------
    def _connect_project(self):
        prj = QgsProject.instance()
        self._conns = []

        def hook(signal, slot):
            # keep the connection handle, not the signal: at program exit the layout manager / themes may be
            # gone already, and disconnecting through their signal then crashes (a handle is safe)
            self._conns.append(signal.connect(slot))

        soon = self._project_changed
        hook(prj.layersAdded, soon)
        hook(prj.layersRemoved, soon)
        hook(prj.readProject, self._project_replaced)
        hook(prj.cleared, self._project_replaced)
        hook(prj.fileNameChanged, self._project_replaced)
        hook(prj.layoutManager().layoutAdded, soon)
        hook(prj.layoutManager().layoutRemoved, soon)
        hook(prj.layoutManager().layoutRenamed, soon)
        hook(prj.mapThemeCollection().mapThemesChanged, soon)

    def _project_changed(self, *_):
        if self.isVisible():
            self._later_rebuild.start()
        else:
            self._dirty = True

    def _project_replaced(self, *_):
        self._files = None
        self._photo_facts = None
        self._scores.clear()
        if self.isVisible():
            QTimer.singleShot(0, self.refresh)
        else:
            self._dirty = True

    def _visible(self, on):
        if on:
            if self._dirty:
                QTimer.singleShot(0, self.refresh)
            self._watch(True)
        else:
            self._watch(False)

    def _watch(self, on):
        dirs = self.watcher.directories()
        if dirs:
            self.watcher.removePaths(dirs)
        if not on:
            return
        want = []
        if self._folder and os.path.isdir(self._folder):
            want.append(self._folder)
            for sub in ("data", "outputs", "exports"):
                p = os.path.join(self._folder, sub)
                if os.path.isdir(p):
                    want.append(p)
        pf = catalog.photos_folder()
        if pf and os.path.isdir(pf):
            want.append(pf)
        if want:
            self.watcher.addPaths(want)

    def watched(self):
        return list(self.watcher.directories())

    def shutdown(self):
        self._tick.stop()
        self._watch(False)
        for conn in getattr(self, "_conns", []):
            try:
                QObject.disconnect(conn)
            except (TypeError, RuntimeError):
                pass
        self._conns = []
        if self._stops_store is not None:
            try:
                self._stops_store.changed.disconnect(self._project_changed)
            except (TypeError, RuntimeError):
                pass
        self.worker.stop()

    # -- reading -------------------------------------------------------------------------------------------------
    def project_folder(self):
        from ..project import project_folder

        prj = QgsProject.instance()
        folder = project_folder(prj)
        if not folder and prj.fileName():
            folder = os.path.dirname(prj.fileName())
        return folder or None

    def refresh(self):
        """Reads everything again: the project now, folders in the worker."""
        self._dirty = False
        self.worker.new_generation()
        self._requested.clear()
        self._facts.clear()
        self._files = None
        self._photo_facts = None
        self._folder = self.project_folder()
        self._scores.clear()
        self.rebuild()
        self.rescan()
        if self.isVisible():
            self._watch(True)

    def rescan(self):
        self._folder = self.project_folder()
        pf = catalog.photos_folder()
        self.stats["scans"] += 1
        if self._folder:
            self.worker.submit("folder", scan.scan_folder, self._folder, scan.MAX_FILES, scan.MAX_DEPTH, (pf,),
                               priority=URGENT)
        else:
            self._files = []
        self.worker.submit("photos", scan.photo_folder_facts, pf, priority=URGENT)
        self._status()

    def _status(self):
        busy = self.worker.pending()
        n = len(self._files or [])
        if self._files is None and self._folder:
            self.status.setText(tr("Reading the project folder…"))
        elif busy:
            self.status.setText(tr("{n} files · reading details…").format(n=format_number(n, 0)))
        else:
            self.status.setText(tr("{n} files").format(n=format_number(n, 0)) if self._folder else "")

    def _job_done(self, job_id, result, gen):
        if gen != self.worker.generation:
            return
        if isinstance(result, Exception):
            result = None
        kind, _, arg = job_id.partition(":")
        if kind == "folder":
            self._files = result or []
            self.rebuild()
            self._watch(self.isVisible())
        elif kind == "photos":
            self._photo_facts = result or {}
            self.rebuild()
        elif kind == "facts":
            self._facts[arg] = result or {}
            for e in self._by_fact.get(arg, []):
                self._apply_facts(e, result or {})
            self._after_update()
        elif kind == "thumb":
            if result is not None and not result.isNull():
                self._pix[arg] = QPixmap.fromImage(result)
                for e in walk(self.entries):
                    if e.data.get("thumb_key") == arg:
                        e.thumb = self._pix[arg]
                    if arg in e.data.get("strip_keys", ()):
                        e.strip = [self._pix[k] for k in e.data["strip_keys"] if k in self._pix]
            self.tree.viewport().update()
        elif kind == "children":
            e = self._lazy.get(arg)
            if e is not None:
                self._fill_children(e, result)
        elif kind in ("dups", "seal"):
            cb = self._jobs_cb.pop(job_id, None)
            if cb is not None:
                cb(result)
        self._status()

    def _after_update(self):
        if self.proxy.words:
            self.proxy.invalidateFilter()
        self.tree.viewport().update()

    # -- facts and badges ----------------------------------------------------------------------------------------
    def _fact_key(self, e):
        return f"{norm(e.path)}|{e.table}"

    def _apply_facts(self, e, facts):
        if not facts:
            return
        f = dict(facts)
        if e.table and f.get("layers"):  # one table of a database
            one = [ly for ly in f["layers"] if ly["name"] == e.table]
            if one:
                ly = one[0]
                f = {"count": ly.get("count"), "crs": ly.get("crs"), "crs_name": ly.get("crs_name"),
                     "projected": ly.get("projected"), "geometry": ly.get("geometry"),
                     "fields": ly.get("fields", []), "table_only": not ly.get("has_geometry", True)}
        keep = {k: e.facts[k] for k in ("size", "mtime") if k in e.facts}
        e.facts.update(f)
        e.facts.update(keep)
        e.badges = self.badges(e)
        e.touch()
        self._want_thumb(e)

    def _used(self):
        if self._used_map is None:
            self._used_map = defaultdict(list)
            for lyr in QgsProject.instance().mapLayers().values():
                p, _t = layer_path(lyr)
                if p:
                    self._used_map[norm(p)].append(lyr)
        return self._used_map

    def badges(self, e):
        """[(text, level)] of an item."""
        f = e.facts
        out = []
        if f.get("missing"):
            out.append((tr("Missing file!"), "bad"))
            return out
        if f.get("error"):
            out.append((tr("Cannot be read"), "bad"))
        spatial = e.layer_type in ("vector", "raster") or e.kind in ("cad",)
        layers = f.get("layers")
        if spatial and not f.get("table_only"):
            if f.get("no_crs"):
                out.append((tr("No CRS!"), "bad"))
            elif layers is not None and not e.data.get("db"):
                geo = [ly for ly in layers if ly.get("has_geometry")]
                if geo and any(not ly.get("crs") for ly in geo):
                    out.append((tr("No CRS!"), "bad"))
                elif geo and any(ly.get("projected") is False for ly in geo):
                    out.append((tr("Not projected"), "warn"))
            elif "crs" in f and e.layer_type == "raster" and not f.get("crs") and "width" in f:
                out.append((tr("No CRS!"), "bad"))
            elif f.get("projected") is False:
                out.append((tr("Not projected"), "warn"))
        if e.kind == "charter":
            score = f.get("score")
            if score is None:
                score = self._scores.get(e.data.get("layer_id"))
            if score is not None:
                out.append((tr("Health {n}%").format(n=score), "good" if score >= 90 else (
                    "warn" if score >= 60 else "bad")))
        if e.data.get("sealed") or (e.path and os.path.isfile(e.path) and os.path.exists(e.path + ".seal.json")):
            out.append((tr("Sealed"), "sealed"))
        if e.path and e.kind in DATA_KINDS + ("database",) and not e.data.get("layer_id"):
            lyrs = self._used().get(norm(e.path), [])
            if e.table:
                lyrs = [ly for ly in lyrs if layer_path(ly)[1] in ("", e.table)]
            from ..charter.model import has_charter

            ch = [ly for ly in lyrs if has_charter(ly)]
            if ch:
                s = self._scores.get(ch[0].id())
                out.append(((tr("Charter {n}%").format(n=s) if s is not None else tr("Data Charter")),
                            "good" if s is None or s >= 90 else ("warn" if s >= 60 else "bad")))
            if lyrs:
                out.append((tr("On the map"), "info"))
        elif e.data.get("layer_id") and e.kind in ("vector", "raster"):
            from ..charter.model import has_charter

            lyr = QgsProject.instance().mapLayer(e.data["layer_id"])
            if lyr is not None and has_charter(lyr):
                s = self._scores.get(lyr.id())
                out.append(((tr("Charter {n}%").format(n=s) if s is not None else tr("Data Charter")),
                            "good" if s is None or s >= 90 else ("warn" if s >= 60 else "bad")))
        return out

    def _want_facts(self, e):
        if not e.path or e.kind not in DATA_KINDS + ("database", "indoor"):
            return
        key = self._fact_key(e)
        self._by_fact[key].append(e)
        if key in self._facts:
            self._apply_facts(e, self._facts[key])
            return
        if e.data.get("layer_id") and e.facts.get("count") is not None:
            self._want_thumb(e)
            return  # a project layer already told what it has
        if key not in self._requested and os.path.exists(e.path):
            self._requested.add(key)
            kind = "database" if e.data.get("db") else e.kind
            self.worker.submit(f"facts:{key}", scan.file_facts, e.path, kind, priority=NORMAL)

    def _want_thumb(self, e):
        if self.delegate is None or not e.path or not os.path.exists(e.path):
            return
        dark = False
        if e.layer_type == "vector" and e.kind in ("vector", "cad") and not e.facts.get("table_only"):
            col = tint(e.kind, dark)
            key = thumbs.file_key(e.path, e.table, col)
            maker, args = thumbs.make_vector, (e.path, e.table, col)
        elif e.layer_type == "raster" and os.path.isfile(e.path):
            key = thumbs.file_key(e.path, "raster")
            maker, args = thumbs.make_raster, (e.path,)
        else:
            return
        e.data["thumb_key"] = key
        if key in self._pix:
            e.thumb = self._pix[key]
            return
        if ("thumb", key) in self._requested:
            return
        self._requested.add(("thumb", key))
        self.worker.submit(f"thumb:{key}", self.cache.get_or_make, key, maker, *args, priority=LOW)

    # -- the tree ------------------------------------------------------------------------------------------------
    def rebuild(self):
        """Builds the sections from what is known now (cheap; folder facts arrive later)."""
        self.stats["rebuilds"] += 1
        self._used_map = None
        if self._expanded is not None:
            self._remember_expanded()
        scroll = self.tree.verticalScrollBar().value()
        self._by_fact = defaultdict(list)
        self._item_of = {}
        self._lazy = {}
        prj = QgsProject.instance()
        self.entries = [self._project_section(prj), self._folder_section(), self._db_section(prj),
                        self._folders_section()]
        for e in walk(self.entries):
            if e.row == "item":
                self._want_facts(e)
                if not e.badges:
                    e.badges = self.badges(e)
        self.model.clear()
        root = self.model.invisibleRootItem()
        for e in self.entries:
            self._add_item(root, e)
        self._restore_expanded()
        self.tree.verticalScrollBar().setValue(scroll)
        self._apply_search()
        self._update_today()
        self._queue_main_jobs(prj)
        self._status()

    def _add_item(self, parent, e):
        it = QStandardItem(e.title)
        it.setData(e, ENTRY_ROLE)
        it.setEditable(False)
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if e.droppable():
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        it.setFlags(flags)
        tip = e.path or e.subtitle
        if tip:
            it.setToolTip(tip)
        self._item_of[id(e)] = it
        parent.appendRow(it)
        for c in e.children:
            self._add_item(it, c)
        if e.lazy is not None and not e.children:
            self._lazy[e.key] = e
            ph = Entry("loading", e.key + ":loading", "", e.kind)
            ph_item = QStandardItem("")
            ph_item.setData(ph, ENTRY_ROLE)
            ph_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            it.appendRow(ph_item)
        return it

    def _project_section(self, prj):
        name = prj.title() or (os.path.splitext(os.path.basename(prj.fileName()))[0] if prj.fileName() else "")
        sec = Entry("section", "sec:project", tr("This project"), "section_project",
                    subtitle=name or tr("Not saved yet"))
        files = self._files or []
        sec.add(catalog.maps_group(prj))
        g = catalog.layouts_group(prj)
        if not g.children:
            g.add(catalog.empty("layout:empty", "layout", tr("Print layouts of this map show here with a picture of "
                                                              "their page."), tr("New layout"), "new_layout"))
        sec.add(g)
        g = catalog.mapbooks_group(prj)
        if g.children:
            sec.add(g)
        sec.add(self._stops_group(prj))
        try:
            g = catalog.charters_group(prj, self._scores)
            if g.children:
                sec.add(g)
        except Exception:  # noqa: BLE001
            pass
        g = catalog.photos_group(self._photo_facts)
        self._photo_thumbs(g)
        sec.add(g)
        for g in (catalog.indoor_group(files, prj), catalog.figures_group(prj), catalog.models_group(files),
                  catalog.styles_group(files), catalog.sealed_group(files)):
            if g.children:
                sec.add(g)
        for g in sec.children:
            if g.kind == "layout" or g.kind == "figure" or g.kind == "mapbook":
                for e in g.children:
                    lay = e.data.get("layout")
                    if lay:
                        self._layout_thumb(e, prj, lay)
        sec.count = sum(g.count or 0 for g in sec.children)
        return sec

    def _stops_group(self, prj):
        try:
            from ..stops.model import store_for

            store = store_for(self.plugin, prj)
            if self._stops_store is not store:
                if self._stops_store is not None:
                    try:
                        self._stops_store.changed.disconnect(self._project_changed)
                    except (TypeError, RuntimeError):
                        pass
                store.changed.connect(self._project_changed)
                self._stops_store = store
            stops = store.stops()
        except Exception:  # noqa: BLE001
            stops = []
        self._stops = stops
        g = catalog.stops_group(stops)
        for e in g.children:
            data = e.data.pop("thumb_bytes", b"")
            if data:
                key = "stop:" + e.data["stop"] + ":" + str(len(data)) + ":" + str(hash(data[:64]))
                if key not in self._pix:
                    pm = QPixmap()
                    if pm.loadFromData(bytes(data)):
                        self._pix[key] = pm
                e.thumb = self._pix.get(key)
        if not g.children:
            g.add(catalog.empty("stop:empty", "stop", tr("Save the view you are looking at as a stop and come back "
                                                         "to it with one click."), tr("New stop"), "new_stop"))
        return g

    def _photo_thumbs(self, g):
        for e in g.children:
            if e.row != "item":
                continue
            paths = (self._photo_facts or {}).get("latest", [])
            e.data["strip_paths"] = paths
            keys = []
            for p in paths:
                key = thumbs.file_key(p, "photo")
                keys.append(key)
                if key not in self._pix and ("thumb", key) not in self._requested:
                    self._requested.add(("thumb", key))
                    self.worker.submit(f"thumb:{key}", self.cache.get_or_make, key, thumbs.make_photo, p,
                                       priority=LOW)
            e.data["strip_keys"] = keys
            e.strip = [self._pix[k] for k in keys if k in self._pix]
            if keys and keys[0] in self._pix:
                e.thumb = self._pix[keys[0]]
            e.data["thumb_key"] = keys[0] if keys else ""

    def _layout_thumb(self, e, prj, name):
        lay = prj.layoutManager().layoutByName(name)
        if lay is None:
            return
        stamp = "|".join(sorted(i.uuid() for i in lay.items() if hasattr(i, "uuid")))
        key = thumbs.file_key(prj.fileName() or "unsaved", "layout:" + name, str(hash(stamp)))
        e.data["thumb_key"] = key
        if key in self._pix:
            e.thumb = self._pix[key]
            return
        if ("thumb", key) in self._requested:
            return
        self._requested.add(("thumb", key))
        self._main.append((self._render_layout, (key, name)))

    def _render_layout(self, key, name):
        lay = QgsProject.instance().layoutManager().layoutByName(name)
        if lay is None:
            return
        img = self.cache.load(key)
        if img is None:
            img = thumbs.layout_image(lay)
            self.cache.save(key, img)
        if img is not None and not img.isNull():
            self._job_done(f"thumb:{key}", img, self.worker.generation)

    def _queue_main_jobs(self, prj):
        try:
            from ..charter.model import charter_layers

            for lyr in charter_layers(prj):
                if lyr.id() not in self._scores and ("score", lyr.id()) not in self._requested:
                    self._requested.add(("score", lyr.id()))
                    self._main.append((self._score_layer, (lyr.id(),)))
        except Exception:  # noqa: BLE001
            pass
        if self._main and not self._tick.isActive():
            self._tick.start()

    def _main_tick(self):
        if not self._main:
            self._tick.stop()
            return
        fn, args = self._main.popleft()
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - one bad layout or layer must not stop the others
            pass

    def _score_layer(self, layer_id):
        lyr = QgsProject.instance().mapLayer(layer_id)
        if lyr is None:
            return
        from ..charter.health import evaluate

        self._scores[layer_id] = evaluate(lyr, limit=20000).score
        for e in walk(self.entries):
            if e.row == "item" and (e.data.get("layer_id") == layer_id or e.path):
                if e.kind == "charter" and e.data.get("layer_id") == layer_id:
                    e.facts["score"] = self._scores[layer_id]
                e.badges = self.badges(e)
                e.touch()
        self._after_update()

    def _folder_section(self):
        folder = self._folder
        sec = Entry("section", "sec:folder", tr("Project folder"), "section_folder", subtitle=folder or "",
                    path=folder or "")
        if not folder:
            sec.add(catalog.empty("folder:empty", "folder", tr("Save this map as a project folder and all its files "
                                                               "will be listed here."), tr("Save as project folder"),
                                  "save_as_folder"))
            sec.count = 0
            return sec
        if self._files is None:
            sec.add(Entry("loading", "folder:loading", "", "folder"))
            return sec
        by = defaultdict(list)
        for rec in self._files:
            by[rec["kind"]].append(rec)
        for kind in ("vector", "raster", "cad", "table", "document"):
            if by.get(kind):
                sec.add(catalog.files_group(f"grp:folder:{kind}", kind, by[kind]))
        if not sec.children:
            sec.add(catalog.empty("folder:none", "folder", tr("No data files in the project folder yet. Copy data "
                                                              "into the project to keep it all together."),
                                  tr("Copy all data into project"), "consolidate"))
        sec.count = sum(g.count or 0 for g in sec.children)
        return sec

    def _db_section(self, prj):
        from ..project import project_gpkg

        sec = Entry("section", "sec:db", tr("Databases"), "section_db")
        seen = set()
        own = project_gpkg(prj)
        if own and os.path.exists(own):
            seen.add(norm(own))
            e = sec.add(catalog.db_entry(own, tr("Project database")))
            self._make_lazy(e)
        for rec in self._files or []:
            if rec["kind"] == "database" and norm(rec["path"]) not in seen:
                seen.add(norm(rec["path"]))
                e = sec.add(catalog.db_entry(rec["path"], rec.get("rel") or tr("In the project folder")))
                self._make_lazy(e)
        for p in catalog.user_databases():
            if norm(p) not in seen:
                seen.add(norm(p))
                e = sec.add(catalog.db_entry(p, os.path.dirname(p)))
                e.data["user_db"] = True
                if os.path.exists(p):
                    self._make_lazy(e)
        try:
            conns = catalog.saved_connections()
        except Exception:  # noqa: BLE001
            conns = []
        for prov, name, uri in conns:
            path = uri.split("|")[0] if prov in ("ogr", "spatialite") else ""
            if path and norm(path) in seen:
                continue
            if path:
                seen.add(norm(path))
            e = sec.add(Entry("item", f"conn:{prov}:{name}", name, "connection",
                              subtitle={"postgres": "PostGIS", "spatialite": "SpatiaLite"}.get(prov, "GeoPackage"),
                              path=path, conn=(prov, name), db=True))
            if path and os.path.exists(path):
                e.facts = {"size": os.path.getsize(path), "mtime": os.path.getmtime(path)}
            e.lazy = (self._conn_tables, (prov, name))
        if not sec.children:
            sec.add(catalog.empty("db:empty", "database", tr("GeoPackages and database connections you use show "
                                                             "here, with their layers inside."),
                                  tr("New GeoPackage"), "new_gpkg"))
        sec.count = sum(1 for e in sec.children if e.row == "item")
        return sec

    def _make_lazy(self, e):
        e.lazy = (scan.vector_facts, (e.path,))

    @staticmethod
    def _conn_tables(prov, name):
        """[(table, schema, kind, uri, crs)] of a saved connection (in the worker thread)."""
        from qgis.core import QgsAbstractDatabaseProviderConnection, QgsProviderRegistry

        md = QgsProviderRegistry.instance().providerMetadata(prov)
        conn = md.findConnection(name)
        out = []
        if conn is None:
            return out
        flags = QgsAbstractDatabaseProviderConnection.TableFlag
        schemas = conn.schemas() if conn.capabilities() & QgsAbstractDatabaseProviderConnection.Capability.Schemas \
            else [""]
        for schema in schemas[:40]:
            for t in conn.tables(schema)[:500]:
                kind = "raster" if t.flags() & flags.Raster else ("vector" if t.flags() & flags.Vector else "table")
                crs = ""
                try:
                    cols = t.geometryColumnTypes()
                    if cols and cols[0].crs.isValid():
                        crs = cols[0].crs.authid()
                except Exception:  # noqa: BLE001
                    pass
                out.append((t.tableName(), schema, kind, conn.tableUri(schema, t.tableName()), crs))
        return out

    def _folders_section(self):
        sec = Entry("section", "sec:folders", tr("Folders"), "section_folders")
        fav = Entry("group", "grp:fav", tr("Favourite folders"), "folder")
        for p in catalog.user_folders():
            e = fav.add(Entry("item", f"fav:{norm(p)}", os.path.basename(p.rstrip("\\/")) or p, "folder",
                              subtitle=p, path=p, user_folder=True))
            if os.path.isdir(p):
                e.lazy = (scan.scan_folder, (p, 600, 2))
            else:
                e.facts = {"missing": True}
                e.badges = [(tr("Missing folder!"), "bad")]
        if not fav.children:
            fav.add(catalog.empty("fav:empty", "folder", tr("Keep the folders you use often one click away."),
                                  tr("Add a folder"), "add_folder"))
        fav.count = sum(1 for e in fav.children if e.row == "item")
        sec.add(fav)
        recent = Entry("group", "grp:recent", tr("Recent data"), "recent")
        for p in [r for r in (QgsSettings().value(RECENT_KEY, [], type=list) or []) if isinstance(r, str)][:15]:
            kind = file_kind(p) or ("vector" if p.lower().endswith(".gdb") else None)
            if kind is None:
                continue
            if kind == "database":
                e = recent.add(catalog.db_entry(p, os.path.dirname(p), "recent"))
                if os.path.exists(p):
                    self._make_lazy(e)
                continue
            rec = {"path": p, "name": os.path.basename(p), "kind": kind, "rel": os.path.dirname(p)}
            if os.path.exists(p):
                rec["size"] = scan.dataset_size(p)
                rec["mtime"] = os.path.getmtime(p)
            e = recent.add(catalog.file_item(rec, "recent"))
            if not os.path.exists(p):
                e.facts = {"missing": True}
        recent.count = len(recent.children)
        if recent.children:
            sec.add(recent)
        sec.count = fav.count + recent.count
        return sec

    # -- lazy children -------------------------------------------------------------------------------------------
    def _on_expanded(self, pix):
        e = pix.data(ENTRY_ROLE)
        if e is None or self._quiet:
            return
        if self._expanded is not None:
            self._expanded.add(e.key)
        if e.lazy is not None and not e.loaded and e.key in self._lazy:
            e.loaded = True
            fn, args = e.lazy
            self.worker.submit(f"children:{e.key}", fn, *args, priority=URGENT)

    def _on_collapsed(self, pix):
        e = pix.data(ENTRY_ROLE)
        if e is not None and self._expanded is not None and not self._quiet:
            self._expanded.discard(e.key)

    def _fill_children(self, e, result):
        item = self._item_of.get(id(e))
        if item is None:
            return
        kids = []
        if e.kind in ("database", "indoor", "recent") or (e.data.get("db") and e.kind != "connection"):
            facts = result or {}
            self._facts[self._fact_key(e)] = facts
            e.facts.update({k: v for k, v in facts.items() if k in ("layers", "rasters")})
            for ly in facts.get("layers", []):
                spatial = ly.get("has_geometry", True)
                c = Entry("item", f"{e.key}/{ly['name']}", ly["name"], "vector" if spatial else "table",
                          subtitle=e.title, path=e.path, uri=f"{e.path}|layername={ly['name']}", provider="ogr",
                          layer_type="vector", table=ly["name"])
                c.facts = {"count": ly.get("count"), "crs": ly.get("crs"), "crs_name": ly.get("crs_name"),
                           "projected": ly.get("projected"), "geometry": ly.get("geometry"),
                           "fields": ly.get("fields", []), "table_only": not spatial}
                kids.append(c)
            for name in facts.get("rasters", []):
                kids.append(Entry("item", f"{e.key}/r/{name}", name, "raster", subtitle=e.title, path=e.path,
                                  uri=f"GPKG:{e.path}:{name}", provider="gdal", layer_type="raster"))
        elif e.kind == "connection":
            for table, schema, kind, uri, crs in result or []:
                c = Entry("item", f"{e.key}/{schema}.{table}", table, kind if kind != "table" else "table",
                          subtitle=schema, uri=uri, provider=e.data["conn"][0],
                          layer_type="raster" if kind == "raster" else "vector")
                c.facts = {"crs": crs} if crs else {}
                kids.append(c)
        elif e.kind == "folder":
            for rec in sorted(result or [], key=lambda r: (r["kind"], r["name"].lower())):
                if rec["kind"] in ("project",):
                    continue
                if rec["kind"] == "database":
                    c = catalog.db_entry(rec["path"], rec.get("rel", ""), e.key)
                    self._make_lazy(c)
                else:
                    c = catalog.file_item(rec, e.key)
                kids.append(c)
            e.facts["files"] = len(kids)
        item.removeRows(0, item.rowCount())
        e.children = kids
        for c in kids:
            c.badges = self.badges(c)
            self._add_item(item, c)
            if c.row == "item":
                self._want_facts(c)
                if c.facts.get("count") is not None or c.layer_type == "raster":
                    self._want_thumb(c)
        if not kids:
            ph = Entry("empty", e.key + ":none", tr("Nothing to show here."), e.kind)
            e.children = [ph]
            self._add_item(item, ph)
        self._after_update()

    def _remember_expanded(self):
        """The open rows; rows not in the tree now (still being read) keep their state."""
        keys, seen = set(), set()

        def visit(parent):
            for r in range(self.proxy.rowCount(parent)):
                ix = self.proxy.index(r, 0, parent)
                e = ix.data(ENTRY_ROLE)
                if e is None:
                    continue
                seen.add(e.key)
                if self.tree.isExpanded(ix):
                    keys.add(e.key)
                    visit(ix)

        if not self.proxy.words:
            visit(QModelIndex())
            closed_parent_kids = {k for k in (self._expanded or set()) if k not in seen}
            self._expanded = keys | closed_parent_kids

    def _restore_expanded(self):
        """Opens the rows that were open; a row seen for the first time opens when it is a section or a short
        group (long lists such as hundreds of documents stay closed)."""
        if self._expanded is None:
            self._expanded = set()
        self.tree.blockSignals(True)
        try:
            def visit(parent):
                for r in range(self.proxy.rowCount(parent)):
                    ix = self.proxy.index(r, 0, parent)
                    e = ix.data(ENTRY_ROLE)
                    if e is None:
                        continue
                    if e.key not in self._known:
                        self._known.add(e.key)
                        if e.row == "section" or (e.row == "group" and (e.count or 0) <= 8
                                                  and e.kind != "document"):
                            self._expanded.add(e.key)
                    open_ = e.key in self._expanded
                    if open_:
                        self.tree.setExpanded(ix, True)
                        visit(ix)
            visit(QModelIndex())
        finally:
            self.tree.blockSignals(False)
        for key in list(self._expanded):  # lazy rows that were open stay open (and read again)
            e = self._lazy.get(key)
            if e is not None and not e.loaded:
                e.loaded = True
                fn, args = e.lazy
                self.worker.submit(f"children:{e.key}", fn, *args, priority=URGENT)

    # -- search --------------------------------------------------------------------------------------------------
    def _apply_search(self):
        words = search_words(self.search.text())
        was = bool(self.proxy.words)
        if words and not was:
            self._remember_expanded()
        self._quiet = True  # opening everything for a search is not the user opening rows
        try:
            self.proxy.set_words(words)
            if words:
                self.tree.expandAll()
            elif was:
                self.tree.collapseAll()
        finally:
            self._quiet = False
        if was and not words:
            self._restore_expanded()

    def visible_entries(self):
        """The item entries shown now (after the search)."""
        out = []

        def visit(parent):
            for r in range(self.proxy.rowCount(parent)):
                ix = self.proxy.index(r, 0, parent)
                e = ix.data(ENTRY_ROLE)
                if e is not None and e.row == "item":
                    out.append(e)
                visit(ix)

        visit(QModelIndex())
        return out

    def find(self, key=None, path=None, title=None):
        for e in walk(self.entries):
            if key is not None and e.key == key:
                return e
            if path is not None and e.path and norm(e.path) == norm(path) and e.row == "item" and (
                    title is None or e.title == title):
                return e
            if title is not None and path is None and key is None and e.title == title and e.row == "item":
                return e
        return None

    def reveal(self, key):
        """Opens and shows the row of a key (the Today chips)."""
        e = self.find(key=key)
        item = self._item_of.get(id(e)) if e is not None else None
        if item is None:
            return
        if self.search.text():
            self.search.clear()
            self._apply_search()
        pix = self.proxy.mapFromSource(item.index())
        parent = pix.parent()
        while parent.isValid():
            self.tree.setExpanded(parent, True)
            parent = parent.parent()
        self.tree.setExpanded(pix, True)
        self.tree.scrollTo(pix, QAbstractItemView.ScrollHint.PositionAtTop)
        self.tree.setCurrentIndex(pix)

    # -- Today ---------------------------------------------------------------------------------------------------
    def today_items(self):
        out = []
        pf = self._photo_facts or {}
        if pf.get("today"):
            out.append(("grp:photos", "camera", tr("{n} new photos").format(n=format_number(pf["today"], 0))))
        edited = 0
        for lyr in QgsProject.instance().mapLayers().values():
            p, _t = layer_path(lyr)
            mod = getattr(lyr, "isModified", lambda: False)()
            if mod or (p and os.path.exists(p) and catalog.today_stamp(os.path.getmtime(p))):
                edited += 1
        if edited:
            out.append(("map:main", "pencil", tr("{n} layers edited").format(n=format_number(edited, 0))))
        today = datetime.date.today().isoformat()
        stops = [s for s in getattr(self, "_stops", []) if (s.created or "").startswith(today)]
        if stops:
            out.append(("grp:stop", "map-pin", tr("{n} new stops").format(n=format_number(len(stops), 0))))
        new_files = [r for r in self._files or [] if catalog.today_stamp(r.get("ctime"))]
        if new_files:
            out.append(("sec:folder", "file-plus", tr("{n} new files").format(n=format_number(len(new_files), 0))))
        return out

    def _update_today(self):
        self.today.set_items(self.today_items(), self.tokens())

    # -- clicks --------------------------------------------------------------------------------------------------
    def _clicked(self, pix):
        e = pix.data(ENTRY_ROLE)
        if e is None:
            return
        if e.row in ("section", "group"):
            self.tree.setExpanded(pix, not self.tree.isExpanded(pix))
        elif e.row == "item" and self.proxy.hasChildren(pix):
            rect = self.tree.visualRect(pix)
            pos = self.tree.viewport().mapFromGlobal(QCursor.pos())
            rtl = self.tree.layoutDirection() == Qt.LayoutDirection.RightToLeft
            near_end = pos.x() < rect.left() + 34 if rtl else pos.x() > rect.right() - 34
            if near_end or e.row == "item" and not e.droppable() and e.kind in ("database", "connection", "folder",
                                                                              "map", "indoor"):
                self.tree.setExpanded(pix, not self.tree.isExpanded(pix))

    def _activated(self, pix):
        e = pix.data(ENTRY_ROLE)
        if e is not None and e.row == "item":
            self.default_action(e)

    def default_action(self, e):
        k = e.kind
        if k == "layout" or (k in ("figure", "mapbook") and e.data.get("layout")):
            return self.open_layout(e.data["layout"])
        if k == "mapbook":
            return self.run_action("map_book")
        if k == "stop":
            return self.go_to_stop(e)
        if k == "theme":
            return self.show_theme(e.data["theme"])
        if k == "charter":
            return self.open_charter(e)
        if k == "figure" and e.data.get("command"):
            return self.run_command(e.data["command"])
        if k == "photos":
            return self.run_action("field_photos")
        if e.data.get("alg"):
            runner = getattr(self.plugin, "runner", None)
            return runner.open_algorithm(e.data["alg"]) if runner is not None else None
        if e.droppable():
            return self.add_to_map(e)
        if e.kind == "style" and e.path:
            return self.apply_style(e)
        if e.path and os.path.exists(e.path):
            return self.open_folder(e)
        return None

    # -- actions -------------------------------------------------------------------------------------------------
    def add_to_map(self, e):
        from .preview import make_layer

        lyr = make_layer(e)
        if lyr is None:
            self._say(tr("«{name}» cannot be opened.").format(name=e.title), warn=True)
            return None
        QgsProject.instance().addMapLayer(lyr)
        return lyr

    def quick_look(self, e, show=True):
        from .preview import QuickLook

        dlg = QuickLook(e, self.tokens(), self.iface.mainWindow(), on_add=self.add_to_map, pixmap=e.thumb)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._keep(dlg)
        if show:
            dlg.show()
            dlg.raise_()
        return dlg

    def properties(self, e, show=True):
        from .preview import PropertiesCard

        used = self.where_used(e) if e.path else None
        dlg = PropertiesCard(e, self.tokens(), self.iface.mainWindow(), used=used, pixmap=e.thumb,
                             on_open_folder=self.open_folder)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._keep(dlg)
        if show:
            dlg.show()
        return dlg

    def where_used(self, e):
        from .usage import where_used

        return where_used(e.path, QgsProject.instance(), e.table or None, getattr(self, "_stops", []))

    def show_where_used(self, e, show=True):
        from .preview import ListDialog, used_html

        used = self.where_used(e)
        dlg = ListDialog(tr("Where is this used?"), tr("Where «{name}» is used in this project").format(name=e.title),
                         used_html(used, self.tokens()), self.tokens(), self.iface.mainWindow())
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._keep(dlg)
        if show:
            dlg.show()
        return dlg, used

    def duplicate_folders(self):
        out = [self._folder] if self._folder else []
        out += [p for p in catalog.user_folders() if os.path.isdir(p)]
        return out

    def find_duplicates(self, e, done=None, show=True):
        """Looks for copies of the file (same size, same content) in the project folder and the favourite
        folders - in the worker; done(paths) is called with the result."""
        from .preview import ListDialog, paths_html

        def finished(paths):
            paths = paths or []
            if show:
                dlg = ListDialog(tr("Find duplicates"), tr("Copies of «{name}»").format(name=e.title),
                                 paths_html(paths, self.tokens()), self.tokens(), self.iface.mainWindow())
                dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
                self._keep(dlg)
                dlg.show()
            if done is not None:
                done(paths)

        job = f"dups:{e.key}:{len(self._jobs_cb)}"
        self._jobs_cb[job] = finished
        self.worker.submit(job, scan.find_duplicates, e.path, self.duplicate_folders(), priority=URGENT)
        self.status.setText(tr("Looking for copies…"))

    def open_folder(self, e, launch=True):
        path = e.path
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if launch:
            if sys.platform.startswith("win") and os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])  # noqa: S603,S607
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        return folder

    def copy_path(self, e):
        QApplication.clipboard().setText(e.path or e.uri)

    def project_layer(self, e, add=True):
        lyrs = layers_of(e.path, QgsProject.instance(), e.table or None) if e.path else []
        if e.data.get("layer_id"):
            lyr = QgsProject.instance().mapLayer(e.data["layer_id"])
            if lyr is not None:
                return lyr
        if lyrs:
            return lyrs[0]
        return self.add_to_map(e) if add else None

    def open_charter(self, e):
        lyr = QgsProject.instance().mapLayer(e.data.get("layer_id", "")) if e.data.get("layer_id") else \
            self.project_layer(e)
        if lyr is None:
            return None
        from ..charter.dialog import open_charter

        return open_charter(self.plugin, layer=lyr)

    def seal(self, e, ask=True, done=None):
        if ask and QMessageBox.question(
                self, tr("Seal"), tr("Seal «{name}»? A seal file and a QR code are written beside it, so anyone can "
                                     "check later that the file was not changed.").format(name=e.title)) \
                != QMessageBox.StandardButton.Yes:
            return
        from ..seal.core import seal_file

        def finished(res):
            ok = isinstance(res, dict)
            if ok:
                e.data["sealed"] = True
                e.badges = self.badges(e)
                e.touch()
                self._after_update()
                self._say(tr("«{name}» is sealed.").format(name=e.title))
            else:
                self._say(tr("«{name}» could not be sealed.").format(name=e.title), warn=True)
            if done is not None:
                done(res)

        job = f"seal:{e.key}"
        self._jobs_cb[job] = finished
        self.worker.submit(job, seal_file, e.path, None, True, priority=URGENT)

    def check_seal(self, e):
        from ..seal import core

        try:
            status, details = core.check_file(e.path)
        except Exception as ex:  # noqa: BLE001
            status, details = None, {"error": str(ex)}
        ok = status == getattr(core, "UNCHANGED", "unchanged")
        res = dict(details or {}, ok=ok, status=status)
        self._say(tr("The seal of «{name}» matches: the file was not changed.").format(name=e.title) if ok else
                  tr("The seal of «{name}» does not match this file.").format(name=e.title), warn=not ok)
        return res

    def rename(self, e, new_name=None):
        from .usage import rename_dataset

        if layers_of(e.path):
            self._say(tr("«{name}» is on the map: remove it from the map before renaming the file.").format(
                name=e.title), warn=True)
            return None
        stem, _ext = os.path.splitext(os.path.basename(e.path))
        if new_name is None:
            new_name, ok = QInputDialog.getText(self, tr("Rename"), tr("New name:"), text=stem)
            if not ok:
                return None
        try:
            new = rename_dataset(e.path, new_name)
        except FileExistsError:
            self._say(tr("A file with this name is already there."), warn=True)
            return None
        except (OSError, ValueError):
            self._say(tr("«{name}» could not be renamed.").format(name=e.title), warn=True)
            return None
        self.rescan()
        return new

    def delete(self, e, ask=True, trash=True):
        from .usage import delete_dataset, parts_of

        if layers_of(e.path):
            self._say(tr("«{name}» is on the map: remove it from the map before deleting the file.").format(
                name=e.title), warn=True)
            return None
        parts = parts_of(e.path)
        if ask and QMessageBox.question(
                self, tr("Delete"), tr("Move «{name}» to the Recycle Bin? ({n} files)").format(
                    name=e.title, n=len(parts))) != QMessageBox.StandardButton.Yes:
            return None
        try:
            gone = delete_dataset(e.path, trash=trash)
        except OSError:
            self._say(tr("«{name}» could not be deleted.").format(name=e.title), warn=True)
            return None
        self.rescan()
        return gone

    def apply_style(self, e):
        lyr = self.iface.activeLayer() if hasattr(self.iface, "activeLayer") else None
        if lyr is None:
            self._say(tr("Choose a layer in the Layers panel first, then apply the style to it."), warn=True)
            return False
        if e.path.lower().endswith(".qml"):
            _msg, ok = lyr.loadNamedStyle(e.path)
        elif e.path.lower().endswith(".sld"):
            ok, _msg = lyr.loadSldStyle(e.path)
        else:
            return False
        lyr.triggerRepaint()
        return ok

    def open_layout(self, name):
        lay = QgsProject.instance().layoutManager().layoutByName(name)
        if lay is not None and hasattr(self.iface, "openLayoutDesigner"):
            self.iface.openLayoutDesigner(lay)
        return lay

    def go_to_stop(self, e):
        from ..stops.actions import go_to

        return go_to(self.plugin, e.data["stop"])

    def show_theme(self, name):
        view = self.iface.layerTreeView() if hasattr(self.iface, "layerTreeView") else None
        if view is None:
            return False
        prj = QgsProject.instance()
        prj.mapThemeCollection().applyTheme(name, prj.layerTreeRoot(), view.layerTreeModel())
        return True

    def run_command(self, cid):
        try:
            from ..commands import CATALOG

            runner = getattr(self.plugin, "runner", None)
            if runner is not None and cid in CATALOG:
                runner.execute(CATALOG[cid])
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def run_action(self, action):
        if not action:
            return
        if action == "add_folder":
            return self.ask_folder()
        if action == "new_gpkg":
            return self.ask_new_gpkg()
        if action == "map_book":
            try:
                from ..mapbook import open_panel

                return open_panel(self.plugin)
            except Exception:  # noqa: BLE001
                return None
        if action == "field_photos":
            from ..field_photos.dock import open_dock

            return open_dock(self.plugin)
        if action == "new_stop":
            from ..stops import new_stop

            return new_stop(self.plugin)
        project = getattr(self.plugin, "project", None)
        if action in ("save_as_folder", "consolidate", "new_project") and project is not None:
            return getattr(project, action)()
        return self.run_command(action)

    # -- adding things -------------------------------------------------------------------------------------------
    def _fill_add_menu(self):
        m = self.add_menu
        m.clear()
        t = self.tokens()
        m.addAction(icons.icon("folder-plus", t["icon"]), tr("Add a folder…"), self.ask_folder)
        m.addAction(icons.icon("database-plus", t["icon"]), tr("Add a database…"), self.ask_database)
        m.addAction(icons.icon("database", t["icon"]), tr("New GeoPackage…"), self.ask_new_gpkg)
        m.addSeparator()
        m.addAction(icons.icon("folder-open", t["icon"]), tr("New project folder…"),
                    lambda: self.run_action("new_project"))

    def ask_folder(self):
        d = QFileDialog.getExistingDirectory(self, tr("A folder to keep in the Vault"), self._folder or "")
        if d:
            self.add_folder(d)

    def add_folder(self, path):
        catalog.add_setting_path(catalog.FOLDERS_KEY, path)
        self.rebuild()

    def remove_folder(self, path):
        catalog.remove_setting_path(catalog.FOLDERS_KEY, path)
        self.rebuild()

    def ask_database(self):
        p, _f = QFileDialog.getOpenFileName(self, tr("Add a database"), self._folder or "",
                                            "GeoPackage / SpatiaLite (*.gpkg *.sqlite *.db)")
        if p:
            self.add_database(p)

    def add_database(self, path):
        catalog.add_setting_path(catalog.DATABASES_KEY, path)
        self.rebuild()

    def remove_database(self, path):
        catalog.remove_setting_path(catalog.DATABASES_KEY, path)
        self.rebuild()

    def ask_new_gpkg(self):
        start = os.path.join(self._folder or os.path.expanduser("~"), "new.gpkg")
        p, _f = QFileDialog.getSaveFileName(self, tr("New GeoPackage"), start, "GeoPackage (*.gpkg)")
        if p:
            self.new_geopackage(p)

    def new_geopackage(self, path):
        from .scan import _gdal

        if not path.lower().endswith(".gpkg"):
            path += ".gpkg"
        _gdal_, ogr, _osr = _gdal()
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
        ok = ds is not None
        ds = None  # noqa: F841 - closes the file
        if ok:
            self.add_database(path)
        return path if ok else None

    # -- the right-click menu ------------------------------------------------------------------------------------
    def selected_entries(self):
        out = []
        for ix in self.tree.selectionModel().selectedIndexes():
            e = ix.data(ENTRY_ROLE)
            if e is not None and e.row == "item":
                out.append(e)
        return out

    def _context_menu(self, pos):
        ix = self.tree.indexAt(pos)
        e = ix.data(ENTRY_ROLE) if ix.isValid() else None
        if e is None or e.row != "item":
            if e is not None and e.key == "sec:folders" or (e is not None and e.key == "grp:fav"):
                m = QMenu(self)
                m.addAction(tr("Add a folder…"), self.ask_folder)
                m.exec(self.tree.viewport().mapToGlobal(pos))
            return
        m = self.build_menu(e)
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def build_menu(self, e):
        t = self.tokens()
        m = QMenu(self)

        def act(icon, text, fn, bold=False):
            a = m.addAction(icons.icon(icon, t["icon"]) if icon else QIcon(), text, fn)
            if bold:
                f = a.font()
                f.setBold(True)
                a.setFont(f)
            return a

        k = e.kind
        if k == "layout" or (k in ("figure", "mapbook") and e.data.get("layout")):
            act("layout-board", tr("Open layout"), lambda: self.open_layout(e.data["layout"]), True)
        if k == "stop":
            act("map-pin", tr("Go to this stop"), lambda: self.go_to_stop(e), True)
        if k == "theme":
            act("stack-2", tr("Show this map theme"), lambda: self.show_theme(e.data["theme"]), True)
        if k == "charter":
            act("license", tr("Open the Data Charter"), lambda: self.open_charter(e), True)
        if k == "photos":
            act("camera", tr("Open Field Photos"), lambda: self.run_action("field_photos"), True)
        if e.data.get("alg"):
            act("player-play" if icons.exists("player-play") else "tools", tr("Run"),
                lambda: self.default_action(e), True)
        if e.droppable():
            act("square-plus", tr("Add to map"), lambda: [self.add_to_map(x) for x in (self.selected_entries() or [e])
                                                           if x.droppable()], k not in ("charter", "photos"))
            act("stops/eye", tr("Quick look"), lambda: self.quick_look(e))
        if e.kind == "style" and e.path:
            act("palette", tr("Apply to the selected layer"), lambda: self.apply_style(e), True)
        if e.path and (k in DATA_KINDS + ("database", "indoor", "photos") or e.layer_type):
            if k != "charter" and e.layer_type == "vector":
                act("license", tr("Data Charter…"), lambda: self.open_charter(e))
        m.addSeparator()
        if e.path:
            act("folder-open", tr("Open its folder"), lambda: self.open_folder(e))
            act("copy", tr("Copy path"), lambda: self.copy_path(e))
        if e.path or e.layer_type:
            act("info-circle", tr("Properties"), lambda: self.properties(e))
        is_file = bool(e.path) and os.path.isfile(e.path) and not e.data.get("layer_id") and k not in (
            "photos", "connection")
        if e.path and k not in ("folder", "photos", "connection"):
            act("map-search", tr("Where is this used?"), lambda: self.show_where_used(e))
        if is_file:
            act("copy", tr("Find duplicates"), lambda: self.find_duplicates(e))
            m.addSeparator()
            if os.path.exists(e.path + ".seal.json"):
                act("shield-check", tr("Check the seal"), lambda: self.check_seal(e))
            else:
                act("file-text-shield", tr("Seal…"), lambda: self.seal(e))
            if not e.table:
                m.addSeparator()
                act("pencil", tr("Rename…"), lambda: self.rename(e))
                act("trash", tr("Delete…"), lambda: self.delete(e))
        if e.data.get("user_folder"):
            m.addSeparator()
            act("x", tr("Remove from the Vault"), lambda: self.remove_folder(e.path))
        if e.data.get("user_db"):
            m.addSeparator()
            act("x", tr("Remove from the Vault"), lambda: self.remove_database(e.path))
        return m

    # -- small things --------------------------------------------------------------------------------------------
    def _keep(self, dlg):
        self._dialogs.append(dlg)
        dlg.destroyed.connect(lambda *_: self._dialogs.remove(dlg) if dlg in self._dialogs else None)

    def _say(self, text, warn=False):
        bar = self.iface.messageBar() if hasattr(self.iface, "messageBar") else None
        if bar is None:
            return
        try:
            from qgis.core import Qgis

            bar.pushMessage(tr("Vault"), text, Qgis.MessageLevel.Warning if warn else Qgis.MessageLevel.Info, 5)
        except Exception:  # noqa: BLE001
            pass


# ---- one pane per window --------------------------------------------------------------------------------------
_DOCK = {}


def open_dock(plugin):
    d = _DOCK.get(id(plugin))
    if d is None:
        d = VaultDock(plugin)
        mw = plugin.iface.mainWindow()
        mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, d)
        try:
            from ..commands import dock_beside_map

            dock_beside_map(mw, d)
        except Exception:  # noqa: BLE001
            pass
        theme = getattr(plugin, "theme", None)
        if theme is not None and hasattr(theme, "on_change"):
            theme.on_change(lambda: _DOCK.get(id(plugin)) is not None and _DOCK[id(plugin)].refresh_theme())
        mode = QgsSettings().value("biruni/vault/mode", "cards")
        if mode == "list":
            d.set_mode("list")
        _DOCK[id(plugin)] = d
    d.show()
    d.raise_()
    if d._dirty:
        d.refresh()
    return d


def toggle_dock(plugin):
    d = _DOCK.get(id(plugin))
    if d is not None and d.isVisible():
        d.hide()
        return d
    return open_dock(plugin)


def close_dock(plugin):
    d = _DOCK.pop(id(plugin), None)
    if d is not None:
        d.shutdown()
        plugin.iface.mainWindow().removeDockWidget(d)
        d.deleteLater()
