"""The list of stops drawn as picture tiles (grid) or rows (list), with groups as collapsible section headers.
Used by the ribbon dropdown (grid, click = go) and by the Manage pane (both views, drag to reorder, F2 renames)."""
from qgis.PyQt.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from qgis.PyQt.QtWidgets import (QAbstractItemView, QLineEdit, QListView, QListWidget, QListWidgetItem,
                                 QStyle, QStyledItemDelegate, QToolTip)

from ..i18n import native_digits, tr
from .model import USER, format_scale

ROLE_KIND = Qt.ItemDataRole.UserRole + 1     # "stop" / "group"
ROLE_STOP = Qt.ItemDataRole.UserRole + 2     # the Stop
ROLE_GROUP = Qt.ItemDataRole.UserRole + 3    # group name
ROLE_NUMBER = Qt.ItemDataRole.UserRole + 4   # 1..9 = its jump key
ROLE_COUNT = Qt.ItemDataRole.UserRole + 5    # stops in a group

TILE_W, TILE_H = 150, 128
PIC_W, PIC_H = 138, 86
ROW_H = 60
ROW_PIC_W, ROW_PIC_H = 76, 48
HEAD_H = 28
COLOURS = ("#E5484D", "#F76B15", "#FFC53D", "#30A46C", "#12A594", "#0090FF", "#8E4EC6", "#D6409F")


def tile_size(viewport_width, spacing):
    """Tiles fill the width: as many columns of at least ~136 px as fit (3 in the dropdown, 2 in a pane)."""
    vw = max(TILE_W, viewport_width - 2)
    cols = max(1, vw // 136)
    w = min(190, (vw - (cols + 1) * 2 * spacing) // cols)
    _pw, ph = pic_size(w)
    return QSize(w, ph + TILE_H - PIC_H)


def pic_size(tile_w):
    pw = max(80, tile_w - 12)
    return pw, int(round(pw * PIC_H / PIC_W))


def _rounded(pm, w, h, radius, dpr):
    out = QPixmap(int(w * dpr), int(h * dpr))
    out.setDevicePixelRatio(dpr)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
    p.setClipPath(path)
    src = pm.size()
    # fill (crop the middle)
    scale = max(w * dpr / max(1, src.width()), h * dpr / max(1, src.height()))
    sw, sh = w * dpr / scale, h * dpr / scale
    p.drawPixmap(QRectF(0, 0, w, h), pm, QRectF((src.width() - sw) / 2, (src.height() - sh) / 2, sw, sh))
    p.end()
    return out


class StopDelegate(QStyledItemDelegate):
    def __init__(self, view, tokens):
        super().__init__(view)
        self.view = view
        self.tokens = tokens
        self._pics = {}

    # ---- sizes ------------------------------------------------------------------------------------------------
    def sizeHint(self, option, index):
        kind = index.data(ROLE_KIND)
        vw = max(120, self.view.viewport().width() - 2 * self.view.spacing() - 2)
        if kind == "group":
            return QSize(vw, HEAD_H)
        if self.view.viewMode() == QListView.ViewMode.IconMode:
            return tile_size(self.view.viewport().width(), self.view.spacing())
        return QSize(vw, ROW_H)

    # ---- pictures ---------------------------------------------------------------------------------------------
    def picture(self, stop, w, h, dpr):
        key = (stop.id, len(stop.thumb), hash(stop.thumb[:64]) if stop.thumb else 0, w, h, dpr, stop.name[:1],
               self.tokens().get("accent"))
        pm = self._pics.get(key)
        if pm is None:
            if stop.thumb:
                src = QPixmap()
                src.loadFromData(stop.thumb)
                if not src.isNull():
                    pm = _rounded(src, w, h, 6, dpr)
            if pm is None:
                pm = self._placeholder(stop, w, h, dpr)
            if len(self._pics) > 400:
                self._pics.clear()
            self._pics[key] = pm
        return pm

    def _placeholder(self, stop, w, h, dpr):
        """No picture yet (a bookmark made in QGIS, or imported): a soft map-like card with the first letter."""
        t = self.tokens()
        out = QPixmap(int(w * dpr), int(h * dpr))
        out.setDevicePixelRatio(dpr)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), 6, 6)
        p.fillPath(path, QColor(t["accent_soft"]))
        p.setClipPath(path)
        pen = QPen(QColor(t["accent"]))
        pen.setWidthF(1.0)
        c = QColor(t["accent"])
        c.setAlpha(45)
        p.setPen(QPen(c, 1.0))
        for i in range(-3, 9):  # faint contour-like lines
            y = h * (i / 6.0)
            p.drawLine(QPoint(0, int(y + h / 3)), QPoint(int(w), int(y)))
        f = QFont(self.view.font())
        f.setPixelSize(int(h * 0.42))
        f.setBold(True)
        p.setFont(f)
        c2 = QColor(t["accent"])
        c2.setAlpha(170)
        p.setPen(c2)
        letter = (stop.name.strip()[:1] or "•").upper()
        p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, letter)
        p.end()
        return out

    # ---- painting ---------------------------------------------------------------------------------------------
    def paint(self, painter, option, index):
        kind = index.data(ROLE_KIND)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            if kind == "group":
                self._paint_group(painter, option, index)
            elif self.view.viewMode() == QListView.ViewMode.IconMode:
                self._paint_tile(painter, option, index)
            else:
                self._paint_row(painter, option, index)
        finally:
            painter.restore()

    def _state(self, option):
        sel = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        return sel, hover

    def _paint_group(self, p, option, index):
        t = self.tokens()
        r = option.rect.adjusted(2, 0, -2, 0)
        rtl = option.direction == Qt.LayoutDirection.RightToLeft
        collapsed = self.view.is_collapsed(index.data(ROLE_GROUP))
        f = QFont(option.font)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(t["text"]))
        # chevron
        cx = r.right() - 8 if rtl else r.left() + 8
        cy = r.center().y()
        pen = QPen(QColor(t["text_dim"]), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        if collapsed:
            d = -1 if rtl else 1
            p.drawLine(QPoint(cx - 2 * d, cy - 4), QPoint(cx + 2 * d, cy))
            p.drawLine(QPoint(cx + 2 * d, cy), QPoint(cx - 2 * d, cy + 4))
        else:
            p.drawLine(QPoint(cx - 4, cy - 2), QPoint(cx, cy + 2))
            p.drawLine(QPoint(cx, cy + 2), QPoint(cx + 4, cy - 2))
        name = index.data(ROLE_GROUP) or tr("Not in a group")
        count = native_digits(str(index.data(ROLE_COUNT) or 0))
        fm = QFontMetrics(f)
        text_r = r.adjusted(0 if rtl else 20, 0, -20 if rtl else 0, 0)
        p.setPen(QColor(t["text"]))
        align = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignAbsolute | (
            Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft)
        name = fm.elidedText(name, Qt.TextElideMode.ElideRight, max(40, text_r.width() - 60))
        p.drawText(text_r, align, name)
        nw = fm.horizontalAdvance(name)
        f2 = QFont(option.font)
        p.setFont(f2)
        fm2 = QFontMetrics(f2)
        cw = fm2.horizontalAdvance(count) + 12
        if rtl:
            chip = QRect(text_r.right() - nw - 8 - cw, cy - 9, cw, 18)
        else:
            chip = QRect(text_r.left() + nw + 8, cy - 9, cw, 18)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t["hover"]))
        p.drawRoundedRect(QRectF(chip), 9, 9)
        p.setPen(QColor(t["text_dim"]))
        p.drawText(chip, Qt.AlignmentFlag.AlignCenter, count)
        # rule
        p.setPen(QPen(QColor(t["border"]), 1))
        if rtl:
            p.drawLine(QPoint(r.left() + 4, cy), QPoint(chip.left() - 8, cy))
        else:
            p.drawLine(QPoint(chip.right() + 8, cy), QPoint(r.right() - 4, cy))

    def _badges(self, p, stop, number, pic_r, rtl):
        t = self.tokens()
        if stop.color:
            # a colour band along the top of the picture
            band = QPainterPath()
            band.addRoundedRect(QRectF(pic_r.left(), pic_r.top(), pic_r.width(), 5), 2.5, 2.5)
            p.fillPath(band, QColor(stop.color))
        if number:
            f = QFont(self.view.font())
            f.setPixelSize(10)
            f.setBold(True)
            p.setFont(f)
            chip = QRectF(pic_r.left() + 5, pic_r.top() + 8, 17, 17)
            if rtl:
                chip.moveRight(pic_r.right() - 5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawRoundedRect(chip, 8.5, 8.5)
            p.setPen(QColor("#FFFFFF"))
            p.drawText(chip, Qt.AlignmentFlag.AlignCenter, native_digits(str(number)))
        if stop.scope == USER:
            # "for all my projects": a small globe in the corner
            g = QRectF(pic_r.right() - 21, pic_r.bottom() - 21, 16, 16)
            if rtl:
                g.moveLeft(pic_r.left() + 5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 215))
            p.drawEllipse(g)
            pen = QPen(QColor(t["accent"] if t.get("accent") else "#0E7C74"), 1.2)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            inner = g.adjusted(3, 3, -3, -3)
            p.drawEllipse(inner)
            p.drawLine(QPoint(int(inner.left()), int(inner.center().y())),
                       QPoint(int(inner.right()), int(inner.center().y())))
            p.drawEllipse(QRectF(inner.center().x() - inner.width() / 4, inner.top(), inner.width() / 2,
                                 inner.height()))

    def _paint_tile(self, p, option, index):
        t = self.tokens()
        stop = index.data(ROLE_STOP)
        sel, hover = self._state(option)
        rtl = option.direction == Qt.LayoutDirection.RightToLeft
        r = option.rect.adjusted(2, 2, -2, -2)
        if sel or hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t["accent_soft"] if sel else t["hover"]))
            p.drawRoundedRect(QRectF(r), 8, 8)
        pw, ph = pic_size(option.rect.width())
        pic_r = QRect(r.left() + (r.width() - pw) // 2, r.top() + 4, pw, ph)
        dpr = self.view.devicePixelRatioF()
        p.drawPixmap(pic_r, self.picture(stop, pw, ph, dpr))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(t["accent"] if sel else t["border"]), 2 if sel else 1))
        p.drawRoundedRect(QRectF(pic_r).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        self._badges(p, stop, index.data(ROLE_NUMBER), pic_r, rtl)
        # name + scale
        f = QFont(option.font)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        fm = QFontMetrics(f)
        name_r = QRect(r.left() + 4, pic_r.bottom() + 4, r.width() - 8, fm.height())
        p.setPen(QColor(t["text"]))
        p.drawText(name_r, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(stop.name, Qt.TextElideMode.ElideRight, name_r.width()))
        f2 = QFont(option.font)
        f2.setPointSizeF(max(7.0, option.font.pointSizeF() - 1.0))
        p.setFont(f2)
        fm2 = QFontMetrics(f2)
        sub = format_scale(stop.scale) or (stop.group if stop.group else "")
        sub_r = QRect(r.left() + 4, name_r.bottom() + 1, r.width() - 8, fm2.height())
        p.setPen(QColor(t["text_dim"]))
        p.drawText(sub_r, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   fm2.elidedText(sub, Qt.TextElideMode.ElideRight, sub_r.width()))

    def _paint_row(self, p, option, index):
        t = self.tokens()
        stop = index.data(ROLE_STOP)
        sel, hover = self._state(option)
        rtl = option.direction == Qt.LayoutDirection.RightToLeft
        r = option.rect.adjusted(2, 1, -2, -1)
        if sel or hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t["accent_soft"] if sel else t["hover"]))
            p.drawRoundedRect(QRectF(r), 6, 6)
        pic_r = QRect(r.left() + 6, r.top() + (r.height() - ROW_PIC_H) // 2, ROW_PIC_W, ROW_PIC_H)
        if rtl:
            pic_r.moveRight(r.right() - 6)
        dpr = self.view.devicePixelRatioF()
        p.drawPixmap(pic_r, self.picture(stop, ROW_PIC_W, ROW_PIC_H, dpr))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(t["accent"] if sel else t["border"]), 1.5 if sel else 1))
        p.drawRoundedRect(QRectF(pic_r).adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
        if stop.color:
            band = QPainterPath()
            band.addRoundedRect(QRectF(pic_r.left(), pic_r.top(), pic_r.width(), 4), 2, 2)
            p.fillPath(band, QColor(stop.color))
        text_r = QRect(r.left() + ROW_PIC_W + 16, r.top() + 6, r.width() - ROW_PIC_W - 26, r.height() - 12)
        if rtl:
            text_r.moveRight(pic_r.left() - 10)
        number = index.data(ROLE_NUMBER)
        align = Qt.AlignmentFlag.AlignAbsolute | (Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft)
        f = QFont(option.font)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        fm = QFontMetrics(f)
        chip_w = 0
        if number:
            chip_w = 26
            f3 = QFont(option.font)
            f3.setPixelSize(10)
            f3.setBold(True)
            p.save()
            p.setFont(f3)
            chip = QRectF(text_r.right() - 18, text_r.top() + 1, 18, 16)
            if rtl:
                chip.moveLeft(text_r.left())
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t["hover"]) if not sel else QColor(t["panel"]))
            p.drawRoundedRect(chip, 4, 4)
            p.setPen(QColor(t["text_dim"]))
            p.drawText(chip, Qt.AlignmentFlag.AlignCenter, native_digits(str(number)))
            p.restore()
        name_r = QRect(text_r.left(), text_r.top(), text_r.width() - chip_w, fm.height())
        if rtl:
            name_r.moveRight(text_r.right())
        p.setPen(QColor(t["text"]))
        p.drawText(name_r, align | Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(stop.name, Qt.TextElideMode.ElideRight, name_r.width()))
        f2 = QFont(option.font)
        f2.setPointSizeF(max(7.0, option.font.pointSizeF() - 0.5))
        p.setFont(f2)
        fm2 = QFontMetrics(f2)
        bits = [b for b in (format_scale(stop.scale), " ".join(stop.note.split())) if b]
        if stop.scope == USER:
            bits.insert(0, tr("All my projects"))
        sub_r = QRect(text_r.left(), name_r.bottom() + 3, text_r.width(), fm2.height())
        p.setPen(QColor(t["text_dim"]))
        p.drawText(sub_r, align | Qt.AlignmentFlag.AlignVCenter,
                   fm2.elidedText("  ·  ".join(bits), Qt.TextElideMode.ElideRight, sub_r.width()))

    # ---- inline rename ----------------------------------------------------------------------------------------
    def createEditor(self, parent, option, index):  # noqa: N802
        if index.data(ROLE_KIND) != "stop":
            return None
        e = QLineEdit(parent)
        e.setFrame(True)
        return e

    def setEditorData(self, editor, index):  # noqa: N802
        editor.setText(index.data(ROLE_STOP).name)
        editor.selectAll()

    def setModelData(self, editor, model, index):  # noqa: N802
        text = editor.text().strip()
        if text and text != index.data(ROLE_STOP).name:
            self.view.renamed.emit(index.data(ROLE_STOP).id, text)

    def updateEditorGeometry(self, editor, option, index):  # noqa: N802
        r = option.rect
        if self.view.viewMode() == QListView.ViewMode.IconMode:
            editor.setGeometry(QRect(r.left() + 4, r.top() + pic_size(r.width())[1] + 6, r.width() - 8, 24))
        else:
            rtl = option.direction == Qt.LayoutDirection.RightToLeft
            g = QRect(r.left() + ROW_PIC_W + 16, r.top() + 4, r.width() - ROW_PIC_W - 26, 24)
            if rtl:
                g.moveLeft(r.left() + 8)
            editor.setGeometry(g)

    def helpEvent(self, event, view, option, index):  # noqa: N802
        """Hover: the note and the scale."""
        if event.type() == QEvent.Type.ToolTip and index.data(ROLE_KIND) == "stop":
            s = index.data(ROLE_STOP)
            lines = [f"<b>{_html(s.name)}</b>"]
            sc = format_scale(s.scale)
            meta = [x for x in (sc, s.group, tr("All my projects") if s.scope == USER else tr("This project")) if x]
            lines.append(f"<span style='color:#888'>{_html(' · '.join(meta))}</span>")
            if s.note:
                lines.append(_html(s.note).replace("\n", "<br>"))
            if s.remembers_layers():
                lines.append(f"<i>{_html(tr('Also shows the layers that were on'))}</i>")
            n = index.data(ROLE_NUMBER)
            if n and self.view.jump_hint:
                lines.append(f"<span style='color:#888'>{_html(self.view.jump_hint.format(n=n))}</span>")
            QToolTip.showText(event.globalPos(), "<br>".join(lines), view)
            return True
        return super().helpEvent(event, view, option, index)


def _html(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class StopListWidget(QListWidget):
    """Stops as tiles or rows, groups as collapsible headers."""

    activated_stop = pyqtSignal(str)          # click (dropdown) / double click (pane)
    renamed = pyqtSignal(str, str)
    reordered = pyqtSignal(list, dict)        # ids in the new order, {id: new group}
    group_toggled = pyqtSignal(str, bool)

    def __init__(self, tokens, grid=True, editable=False, single_click=False, parent=None):
        super().__init__(parent)
        self.tokens = tokens
        self.editable = editable
        self.single_click = single_click
        self.jump_hint = ""
        self.headerless_ungrouped = True  # the stops without a group open the list, with no header
        self.collapsed = set()
        self._drop_line = None
        self.setObjectName("StopList")
        self.setItemDelegate(StopDelegate(self, tokens))
        self.setMouseTracking(True)
        self.setUniformItemSizes(False)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSpacing(3)
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection if editable
                              else QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed if editable
                             else QAbstractItemView.EditTrigger.NoEditTriggers)
        if editable:
            self.setDragEnabled(True)
            self.setAcceptDrops(True)
            self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
            self.setDefaultDropAction(Qt.DropAction.CopyAction)
            self.setDropIndicatorShown(False)
        self.set_grid(grid)
        self.itemClicked.connect(self._clicked)
        self.itemDoubleClicked.connect(self._double)

    def set_grid(self, grid):
        self.setViewMode(QListView.ViewMode.IconMode if grid else QListView.ViewMode.ListMode)
        self.setFlow(QListView.Flow.LeftToRight if grid else QListView.Flow.TopToBottom)
        self.setWrapping(grid)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSpacing(3)
        self.doItemsLayout()

    def is_grid(self):
        return self.viewMode() == QListView.ViewMode.IconMode

    def is_collapsed(self, group):
        return group in self.collapsed

    # ---- filling ----------------------------------------------------------------------------------------------
    def populate(self, stops, show_groups=True, numbers=None, keep_selection=True):
        """stops in order; numbers: {stop id: 1..9}."""
        selected = {i.data(ROLE_STOP).id for i in self.selectedItems() if i.data(ROLE_KIND) == "stop"} \
            if keep_selection else set()
        self.blockSignals(True)
        self.clear()
        numbers = numbers or {}
        groups = {}
        for s in stops:
            groups.setdefault(s.group, []).append(s)
        use_headers = show_groups and any(g for g in groups)
        order = list(groups) if use_headers else [None]
        if use_headers and "" in groups:  # stops without a group first
            order = [""] + [g for g in order if g != ""]
        for g in order:
            members = groups[g] if g is not None else stops
            if use_headers and (g != "" or not self.headerless_ungrouped):
                h = QListWidgetItem()
                h.setData(ROLE_KIND, "group")
                h.setData(ROLE_GROUP, g)
                h.setData(ROLE_COUNT, len(members))
                h.setFlags(Qt.ItemFlag.ItemIsEnabled | (Qt.ItemFlag.ItemIsDropEnabled if self.editable
                                                        else Qt.ItemFlag.NoItemFlags))
                self.addItem(h)
            for s in members:
                it = QListWidgetItem(s.name)
                it.setData(ROLE_KIND, "stop")
                it.setData(ROLE_STOP, s)
                it.setData(ROLE_GROUP, s.group)
                if s.id in numbers:
                    it.setData(ROLE_NUMBER, numbers[s.id])
                flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if self.editable:
                    flags |= Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDragEnabled
                it.setFlags(flags)
                self.addItem(it)
                if s.id in selected:
                    it.setSelected(True)
                if use_headers and g and g in self.collapsed:
                    it.setHidden(True)
        self.blockSignals(False)
        self.doItemsLayout()

    def stop_items(self):
        return [self.item(i) for i in range(self.count()) if self.item(i).data(ROLE_KIND) == "stop"]

    def selected_ids(self):
        return [i.data(ROLE_STOP).id for i in self.selectedItems() if i.data(ROLE_KIND) == "stop"]

    def select_ids(self, ids):
        ids = set(ids)
        for it in self.stop_items():
            it.setSelected(it.data(ROLE_STOP).id in ids)

    def toggle_group(self, group):
        collapse = group not in self.collapsed
        if collapse:
            self.collapsed.add(group)
        else:
            self.collapsed.discard(group)
        for it in self.stop_items():
            if it.data(ROLE_GROUP) == group:
                it.setHidden(collapse)
        self.viewport().update()
        self.group_toggled.emit(group, collapse)

    # ---- mouse ------------------------------------------------------------------------------------------------
    def _clicked(self, item):
        if item.data(ROLE_KIND) == "group":
            self.toggle_group(item.data(ROLE_GROUP))
        elif self.single_click:
            self.activated_stop.emit(item.data(ROLE_STOP).id)

    def _double(self, item):
        if item.data(ROLE_KIND) == "stop" and not self.single_click:
            self.activated_stop.emit(item.data(ROLE_STOP).id)

    def resizeEvent(self, e):  # noqa: N802 - full-width headers follow the width
        super().resizeEvent(e)
        self.doItemsLayout()

    # ---- drag to reorder (and into another group) ---------------------------------------------------------------
    def _drop_target(self, pos):
        """(row to insert before, group) for a drop at pos."""
        it = self.itemAt(pos)
        if it is None:
            return self.count(), self._group_at(self.count() - 1)
        row = self.row(it)
        r = self.visualItemRect(it)
        if it.data(ROLE_KIND) == "group":
            return row + 1, it.data(ROLE_GROUP)
        if self.is_grid():
            after = pos.x() > r.center().x()
            if self.layoutDirection() == Qt.LayoutDirection.RightToLeft:
                after = not after
        else:
            after = pos.y() > r.center().y()
        return row + (1 if after else 0), it.data(ROLE_GROUP)

    def _group_at(self, row):
        for i in range(row, -1, -1):
            it = self.item(i)
            if it is not None and it.data(ROLE_KIND) == "group":
                return it.data(ROLE_GROUP)
        return None

    def dragEnterEvent(self, e):  # noqa: N802
        if e.source() is self:
            e.setDropAction(Qt.DropAction.CopyAction)
            e.accept()
        else:
            e.ignore()

    def dragMoveEvent(self, e):  # noqa: N802
        if e.source() is not self:
            e.ignore()
            return
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        row, _g = self._drop_target(pos)
        self._drop_line = self._line_for(row)
        self.viewport().update()
        e.setDropAction(Qt.DropAction.CopyAction)
        e.accept()

    def dragLeaveEvent(self, e):  # noqa: N802
        self._drop_line = None
        self.viewport().update()
        super().dragLeaveEvent(e)

    def _line_for(self, row):
        if self.count() == 0:
            return None
        if row < self.count():
            r = self.visualItemRect(self.item(row))
            before = True
        else:
            r = self.visualItemRect(self.item(self.count() - 1))
            before = False
        if self.is_grid() and self.item(min(row, self.count() - 1)).data(ROLE_KIND) == "stop":
            rtl = self.layoutDirection() == Qt.LayoutDirection.RightToLeft
            x = (r.left() if before != rtl else r.right())
            return QRect(x - 1, r.top() + 4, 3, r.height() - 8)
        y = r.top() if before else r.bottom()
        return QRect(r.left() + 4, y - 1, r.width() - 8, 3)

    def dropEvent(self, e):  # noqa: N802
        self._drop_line = None
        self.viewport().update()
        if e.source() is not self:
            e.ignore()
            return
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        row, group = self._drop_target(pos)
        moving = self.selected_ids()
        order, regroup = reorder_plan(self._layout(), moving, row, group)
        e.setDropAction(Qt.DropAction.CopyAction)  # the list is rebuilt from the store: Qt must not remove rows
        e.accept()
        if moving:
            self.reordered.emit(order, regroup)

    def _layout(self):
        out = []
        for i in range(self.count()):
            it = self.item(i)
            if it.data(ROLE_KIND) == "group":
                out.append(("group", it.data(ROLE_GROUP)))
            else:
                s = it.data(ROLE_STOP)
                out.append(("stop", s.id, s.group))
        return out

    def paintEvent(self, e):  # noqa: N802
        super().paintEvent(e)
        if self._drop_line is not None:
            p = QPainter(self.viewport())
            p.fillRect(self._drop_line, QBrush(QColor(self.tokens()["accent"])))
            p.end()

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.state() != QAbstractItemView.State.EditingState:
            it = self.currentItem()
            if it is not None and it.data(ROLE_KIND) == "stop":
                self.activated_stop.emit(it.data(ROLE_STOP).id)
                return
            if it is not None and it.data(ROLE_KIND) == "group":
                self.toggle_group(it.data(ROLE_GROUP))
                return
        super().keyPressEvent(e)


def reorder_plan(layout, moving, row, group):
    """The new order of stop ids after dropping `moving` before position `row` of the layout (a list of
    ("group", name) and ("stop", id, group) entries), and the stops whose group changes ({id: group}).
    group is the section the drop lands in (None when the list has no sections)."""
    moving = [m for m in moving]
    moving_set = set(moving)
    before = [e for e in layout[:row] if e[0] == "stop" and e[1] not in moving_set]
    after = [e for e in layout[row:] if e[0] == "stop" and e[1] not in moving_set]
    order = [e[1] for e in before] + moving + [e[1] for e in after]
    regroup = {}
    if group is not None:
        current = {e[1]: e[2] for e in layout if e[0] == "stop"}
        regroup = {m: group for m in moving if current.get(m, "") != group}
    return order, regroup
