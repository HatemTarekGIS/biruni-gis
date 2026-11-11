"""Manage stops: a panel with every stop - search (Arabic-aware), list or picture grid, groups as collapsible
sections, drag to reorder (or into another group), F2 renames, a card to edit the selected stop (name, group, note,
colour, layers, where it is kept) and to update it to the current view or picture, duplicate, send to a phone or
delete - with Undo."""
from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDockWidget, QFrame, QHBoxLayout,
                                 QInputDialog, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QSizePolicy,
                                 QToolButton, QVBoxLayout, QWidget)

from .. import icons
from ..i18n import native_digits, tr
from .gallery import filter_label, filtered
from .model import PROJECT, USER, store_for
from .stoplist import COLOURS, ROLE_KIND, ROLE_STOP, StopListWidget

KEY = "biruni/stops/pane/"


def pane_qss(t):
    return f"""
#StopsPane QLineEdit, #StopsPane QPlainTextEdit, #StopsPane QComboBox {{
    border: 1px solid {t['border']}; border-radius: 6px; padding: 4px 7px; background: {t['base']}; color: {t['text']};
}}
#StopsPane QLineEdit:focus, #StopsPane QPlainTextEdit:focus, #StopsPane QComboBox:focus {{ border-color: {t['accent']}; }}
#StopsPane QListWidget#StopList {{ background: {t['base']}; border: 1px solid {t['border']}; border-radius: 8px;
    outline: none; }}
#StopsPane QToolButton#PaneTool {{ border: 1px solid transparent; border-radius: 6px; padding: 4px; background: transparent; }}
#StopsPane QToolButton#PaneTool:hover {{ background: {t['hover']}; }}
#StopsPane QToolButton#PaneTool:checked {{ background: {t['accent_soft']}; border-color: {t['accent_soft']}; }}
#StopsPane QToolButton#PaneTool::menu-indicator {{ image: none; width: 0; }}
#StopsPane QToolButton#PaneFilter {{ border: 1px solid {t['border']}; border-radius: 6px; padding: 4px 8px;
    background: {t['base']}; color: {t['text']}; }}
#StopsPane QToolButton#PaneFilter::menu-indicator {{ image: none; width: 0; }}
#StopsPane QPushButton {{ border: 1px solid {t['border']}; border-radius: 6px; padding: 5px 10px; background: {t['button']};
    color: {t['text']}; }}
#StopsPane QPushButton:hover {{ border-color: {t['accent']}; }}
#StopsPane QPushButton:disabled {{ color: {t['text_disabled']}; }}
#StopsPane QPushButton#GpsPrimary {{ background: {t['accent']}; color: {t['accent_text']}; border: none; font-weight: 600; }}
#StopsPane QPushButton#GpsPrimary:hover {{ background: {t['accent_hover']}; }}
#StopsPane QPushButton#Danger:hover {{ border-color: #D64545; color: #D64545; }}
#StopsPane QLabel#CardTitle {{ font-weight: 600; color: {t['text']}; }}
#StopsPane QLabel#FieldKey {{ color: {t['text_dim']}; }}
#StopsPane QFrame#UndoBar {{ background: {t['tooltip']}; border-radius: 8px; }}
#StopsPane QFrame#UndoBar QLabel {{ color: {t['tooltip_text']}; }}
#StopsPane QFrame#UndoBar QPushButton {{ background: transparent; border: none; color: {t['accent_hover']};
    font-weight: 600; padding: 3px 8px; }}
#StopsPane QToolButton#Swatch {{ border: 2px solid transparent; border-radius: 10px; padding: 0; }}
#StopsPane QToolButton#Swatch:checked {{ border-color: {t['text']}; }}
"""


def _swatch_icon(colour, size=16):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if colour:
        p.setBrush(QColor(colour))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(1, 1, size - 2, size - 2)
    else:  # "no colour": a ring with a slash
        p.setPen(QColor("#8A949D"))
        p.drawEllipse(2, 2, size - 4, size - 4)
        p.drawLine(4, size - 4, size - 4, 4)
    p.end()
    return QIcon(pm)


class StopsPane(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("Map Stops"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.store = store_for(plugin)
        self.setObjectName("BiruniMapStops")
        self.value = QgsSettings().value(KEY + "filter", "all") or "all"
        self._undo = None
        self._loading = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(0)
        self._refresh_timer.timeout.connect(self.refresh)
        self._note_timer = QTimer(self)
        self._note_timer.setSingleShot(True)
        self._note_timer.setInterval(700)
        self._note_timer.timeout.connect(self._save_note)
        self._build()
        self.store.changed.connect(self._refresh_timer.start)
        self.refresh()

    def _tokens(self):
        from .gallery import _tokens

        return _tokens(self.plugin)

    # ---- building -----------------------------------------------------------------------------------------------
    def _build(self):
        body = QWidget()
        body.setObjectName("StopsPane")
        self.body = body
        v = QVBoxLayout(body)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(7)

        top = QHBoxLayout()
        top.setSpacing(4)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search stops"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        top.addWidget(self.search, 1)
        self.filter_btn = QToolButton()
        self.filter_btn.setObjectName("PaneFilter")
        self.filter_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.filter_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.filter_btn.setIconSize(QSize(12, 12))
        self.filter_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft
                                           if self.layoutDirection() == Qt.LayoutDirection.LeftToRight
                                           else Qt.LayoutDirection.LeftToRight)  # the arrow after the text
        fm = QMenu(self.filter_btn)
        fm.aboutToShow.connect(lambda: (fm.clear(), self._fill_filter(fm)))
        self.filter_btn.setMenu(fm)
        top.addWidget(self.filter_btn)
        self.list_btn = self._tool("layout-list", tr("List"), checkable=True)
        self.grid_btn = self._tool("layout-grid", tr("Pictures"), checkable=True)
        self.list_btn.clicked.connect(lambda: self.set_grid(False))
        self.grid_btn.clicked.connect(lambda: self.set_grid(True))
        top.addWidget(self.list_btn)
        top.addWidget(self.grid_btn)
        v.addLayout(top)

        acts = QHBoxLayout()
        acts.setSpacing(4)
        self.new_btn = QPushButton(tr("New stop"))
        self.new_btn.setObjectName("GpsPrimary")
        self.new_btn.setToolTip(tr("Save what the map shows now (Ctrl+Shift+B)"))
        self.new_btn.clicked.connect(self.new_stop)
        acts.addWidget(self.new_btn)
        self.tour_btn = QPushButton(tr("Play as a tour"))
        self.tour_btn.clicked.connect(self.play)
        acts.addWidget(self.tour_btn)
        acts.addStretch(1)
        self.more_btn = self._tool("stops/dots", tr("More"))
        mm = QMenu(self.more_btn)
        mm.aboutToShow.connect(lambda: (mm.clear(), self._fill_more(mm)))
        self.more_btn.setMenu(mm)
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        acts.addWidget(self.more_btn)
        v.addLayout(acts)

        grid = QgsSettings().value(KEY + "grid", False, type=bool)
        self.list = StopListWidget(self._tokens, grid=grid, editable=True, single_click=False)
        from .actions import jump_hint

        self.list.jump_hint = jump_hint()
        self.list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.list.setMinimumHeight(180)
        self.list.activated_stop.connect(self.go)
        self.list.renamed.connect(lambda sid, name: self.store.rename(sid, name))
        self.list.reordered.connect(self._reordered)
        self.list.itemSelectionChanged.connect(self._selection)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context)
        v.addWidget(self.list, 1)
        self.list_btn.setChecked(not grid)
        self.grid_btn.setChecked(grid)

        self.empty = QLabel()
        self.empty.setObjectName("GpsHint")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.empty)

        self.card = self._build_card()
        v.addWidget(self.card)
        self.multi = self._build_multi()
        v.addWidget(self.multi)

        self.undo_bar = QFrame()
        self.undo_bar.setObjectName("UndoBar")
        ub = QHBoxLayout(self.undo_bar)
        ub.setContentsMargins(10, 4, 4, 4)
        self.undo_text = QLabel()
        ub.addWidget(self.undo_text, 1)
        undo_btn = QPushButton(tr("Undo"))
        undo_btn.clicked.connect(self.undo)
        ub.addWidget(undo_btn)
        self.undo_bar.hide()
        self._undo_timer = QTimer(self)
        self._undo_timer.setSingleShot(True)
        self._undo_timer.timeout.connect(self.undo_bar.hide)
        v.addWidget(self.undo_bar)

        for seq, fn in ((QKeySequence.StandardKey.Delete, self.delete_selected),
                        (QKeySequence("Ctrl+Up"), lambda: self.move_selected(-1)),
                        (QKeySequence("Ctrl+Down"), lambda: self.move_selected(1)),
                        (QKeySequence.StandardKey.Undo, self.undo)):
            sc = QShortcut(seq, self.list)
            sc.setContext(Qt.ShortcutContext.WidgetShortcut)
            sc.activated.connect(fn)
        self.setWidget(body)
        self.setMinimumWidth(320)
        self.refresh_theme()

    def _tool(self, icon_name, tip, checkable=False):
        b = QToolButton()
        b.setObjectName("PaneTool")
        b.setToolTip(tip)
        b.setCheckable(checkable)
        b.setIconSize(QSize(18, 18))
        b._icon_name = icon_name
        return b

    def _build_card(self):
        card = QFrame()
        card.setObjectName("GpsCard")
        c = QVBoxLayout(card)
        c.setContentsMargins(10, 8, 10, 10)
        c.setSpacing(6)
        self.name = QLineEdit()
        self.name.setPlaceholderText(tr("Name"))
        self.name.editingFinished.connect(self._save_name)
        f = self.name.font()
        f.setBold(True)
        self.name.setFont(f)
        c.addWidget(self.name)
        row = QHBoxLayout()
        row.setSpacing(6)
        k = QLabel(tr("Group"))
        k.setObjectName("FieldKey")
        row.addWidget(k)
        self.group = QComboBox()
        self.group.setEditable(True)
        self.group.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.group.lineEdit().setPlaceholderText(tr("No group"))
        self.group.lineEdit().editingFinished.connect(self._save_group)
        self.group.activated.connect(lambda _i: self._save_group())
        row.addWidget(self.group, 1)
        c.addLayout(row)
        sw = QHBoxLayout()
        sw.setSpacing(2)
        k = QLabel(tr("Colour"))
        k.setObjectName("FieldKey")
        sw.addWidget(k)
        sw.addSpacing(4)
        self.swatches = []
        for col in ("",) + COLOURS:
            b = QToolButton()
            b.setObjectName("Swatch")
            b.setCheckable(True)
            b.setAutoRaise(True)
            b.setIcon(_swatch_icon(col))
            b.setIconSize(QSize(16, 16))
            b.setFixedSize(QSize(22, 22))
            b.setToolTip(tr("No colour") if not col else col)
            b.clicked.connect(lambda _=False, colour=col: self._save_colour(colour))
            b._colour = col
            self.swatches.append(b)
            sw.addWidget(b)
        sw.addStretch(1)
        c.addLayout(sw)
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText(tr("Note - shown when you hover the stop and as the caption of the tour"))
        self.note.setFixedHeight(58)
        self.note.textChanged.connect(lambda: None if self._loading else self._note_timer.start())
        c.addWidget(self.note)
        self.layers = QCheckBox(tr("Also show the layers that were on"))
        self.layers.setToolTip(tr("Going to the stop switches the layers (and their styles) back to how they were "
                                  "when it was saved - a map theme of the project."))
        self.layers.toggled.connect(self._save_layers)
        c.addWidget(self.layers)
        row = QHBoxLayout()
        k = QLabel(tr("Kept in"))
        k.setObjectName("FieldKey")
        row.addWidget(k)
        self.scope = QComboBox()
        self.scope.addItem(tr("This project"), PROJECT)
        self.scope.addItem(tr("All my projects"), USER)
        self.scope.activated.connect(lambda _i: self._save_scope())
        row.addWidget(self.scope, 1)
        c.addLayout(row)
        b1 = QHBoxLayout()
        b1.setSpacing(4)
        self.go_btn = QPushButton(tr("Go"))
        self.go_btn.setObjectName("GpsPrimary")
        self.go_btn.clicked.connect(lambda: self.go(self.current_id()))
        self.update_btn = QPushButton(tr("Update to current view"))
        self.update_btn.setToolTip(tr("The stop takes what the map shows now: place, scale, rotation, time, "
                                      "layers and picture."))
        self.update_btn.clicked.connect(self.update_view)
        self.pic_btn = QPushButton(tr("New picture"))
        self.pic_btn.setToolTip(tr("Draw the stop's picture again from the map as it is now"))
        self.pic_btn.clicked.connect(self.update_picture)
        b1.addWidget(self.go_btn)
        b1.addWidget(self.update_btn, 1)
        b1.addWidget(self.pic_btn)
        c.addLayout(b1)
        b2 = QHBoxLayout()
        b2.setSpacing(4)
        dup = QPushButton(tr("Duplicate"))
        dup.clicked.connect(lambda: self.store.duplicate(self.current_id()))
        phone = QPushButton(tr("Send to a phone"))
        phone.clicked.connect(lambda: self._send(self.current_id()))
        dele = QPushButton(tr("Delete"))
        dele.setObjectName("Danger")
        dele.clicked.connect(self.delete_selected)
        b2.addWidget(dup)
        b2.addWidget(phone, 1)
        b2.addWidget(dele)
        c.addLayout(b2)
        return card

    def _build_multi(self):
        card = QFrame()
        card.setObjectName("GpsCard")
        c = QVBoxLayout(card)
        c.setContentsMargins(10, 8, 10, 10)
        self.multi_title = QLabel()
        self.multi_title.setObjectName("CardTitle")
        c.addWidget(self.multi_title)
        row = QHBoxLayout()
        k = QLabel(tr("Group"))
        k.setObjectName("FieldKey")
        row.addWidget(k)
        self.multi_group = QComboBox()
        self.multi_group.setEditable(True)
        self.multi_group.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.multi_group.lineEdit().setPlaceholderText(tr("No group"))
        row.addWidget(self.multi_group, 1)
        put = QPushButton(tr("Put in group"))
        put.clicked.connect(lambda: self.store.set_group(self.list.selected_ids(),
                                                          self.multi_group.currentText().strip()))
        row.addWidget(put)
        c.addLayout(row)
        sw = QHBoxLayout()
        sw.setSpacing(2)
        k = QLabel(tr("Colour"))
        k.setObjectName("FieldKey")
        sw.addWidget(k)
        for col in ("",) + COLOURS:
            b = QToolButton()
            b.setObjectName("Swatch")
            b.setAutoRaise(True)
            b.setIcon(_swatch_icon(col))
            b.setIconSize(QSize(16, 16))
            b.setFixedSize(QSize(22, 22))
            b.clicked.connect(lambda _=False, colour=col: [self.store.update(i, color=colour)
                                                            for i in self.list.selected_ids()])
            sw.addWidget(b)
        sw.addStretch(1)
        c.addLayout(sw)
        b = QHBoxLayout()
        tour = QPushButton(tr("Tour of these"))
        tour.clicked.connect(lambda: self.play(self.list.selected_ids()))
        exp = QPushButton(tr("Export…"))
        exp.clicked.connect(lambda: self._export(self.list.selected_ids()))
        dele = QPushButton(tr("Delete"))
        dele.setObjectName("Danger")
        dele.clicked.connect(self.delete_selected)
        b.addWidget(tour)
        b.addWidget(exp)
        b.addStretch(1)
        b.addWidget(dele)
        c.addLayout(b)
        return card

    def refresh_theme(self):
        from ..tracking import panel_qss

        t = self._tokens()
        self.body.setStyleSheet(panel_qss(t).replace("#BiruniGps", "#StopsPane") + pane_qss(t))
        for b in (self.list_btn, self.grid_btn, self.more_btn):
            b.setIcon(icons.icon(b._icon_name, t["icon"]))
        self.filter_btn.setIcon(icons.icon("chevron-down", t["text_dim"]))
        self.list.viewport().update()

    # ---- filling ------------------------------------------------------------------------------------------------
    def _fill_filter(self, m):
        for value, text in (("all", tr("All")), ("project", tr("This project")), ("user", tr("All my projects"))):
            a = m.addAction(text)
            a.setCheckable(True)
            a.setChecked(self.value == value)
            a.triggered.connect(lambda _=False, val=value: self.set_filter(val))
        groups = self.store.groups()
        if groups:
            m.addSeparator()
            for g in groups:
                a = m.addAction(g)
                a.setCheckable(True)
                a.setChecked(self.value == "group:" + g)
                a.triggered.connect(lambda _=False, val="group:" + g: self.set_filter(val))

    def _fill_more(self, m):
        t = self._tokens()

        def add(icon, text, fn, enabled=True):
            a = m.addAction(icons.icon(icon, t["icon"], t["icon_disabled"]), text)
            a.setEnabled(enabled)
            a.triggered.connect(lambda _=False: fn())

        has = bool(self.store.stops())
        add("file-export", tr("Export all…"), lambda: self._export(None), has)
        add("file-export", tr("Export selected…"), lambda: self._export(self.list.selected_ids()),
            bool(self.list.selected_ids()))
        add("file-import", tr("Import…"), self._import)
        m.addSeparator()
        add("device-mobile-pin", tr("Send this view to a phone…"), lambda: self._send(None))
        add("settings", tr("Options…"), self._options)

    def set_filter(self, value):
        self.value = value
        QgsSettings().setValue(KEY + "filter", value)
        self.refresh()

    def set_grid(self, grid):
        QgsSettings().setValue(KEY + "grid", bool(grid))
        self.list_btn.setChecked(not grid)
        self.grid_btn.setChecked(grid)
        self.list.set_grid(grid)
        self.refresh()

    def refresh(self):
        if self.list.state() == QAbstractItemView.State.EditingState:
            self._refresh_timer.start(300)
            return
        self.filter_btn.setText(filter_label(self.value))
        stops = filtered(self.store, self.value, self.search.text())
        from .actions import numbers

        self.list.populate(stops, show_groups=not self.value.startswith("group:"), numbers=numbers(self.store))
        has_any = bool(self.store.stops())
        self.tour_btn.setEnabled(has_any)
        self.empty.setVisible(not stops)
        if not stops:
            self.empty.setText(tr("No stop matches «{q}».").format(q=self.search.text()) if self.search.text()
                               else tr("No stops yet. «New stop» saves what the map shows now - the place, the "
                                       "scale and a picture."))
        self._selection()

    def current_id(self):
        ids = self.list.selected_ids()
        return ids[0] if len(ids) == 1 else None

    def _selection(self):
        ids = self.list.selected_ids()
        self.card.setVisible(len(ids) == 1)
        self.multi.setVisible(len(ids) > 1)
        if len(ids) > 1:
            self.multi_title.setText(tr("{n} stops selected").format(n=native_digits(str(len(ids)))))
            self._fill_groups(self.multi_group, "")
        if len(ids) != 1:
            return
        s = self.store.get(ids[0])
        if s is None:
            return
        self._loading = True
        try:
            if not self.name.hasFocus():
                self.name.setText(s.name)
            self._fill_groups(self.group, s.group)
            if not self.note.hasFocus():
                self.note.setPlainText(s.note)
            for b in self.swatches:
                b.setChecked((b._colour or "").lower() == (s.color or "").lower())
            self.layers.setChecked(s.remembers_layers())
            self.scope.setCurrentIndex(0 if s.scope == PROJECT else 1)
        finally:
            self._loading = False

    def _fill_groups(self, combo, current):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("")
        for g in self.store.groups():
            combo.addItem(g)
        combo.setEditText(current)
        combo.blockSignals(False)

    # ---- editing ------------------------------------------------------------------------------------------------
    def _save_name(self):
        sid = self.current_id()
        text = self.name.text().strip()
        if sid and text:
            s = self.store.get(sid)
            if s and s.name != text:
                self.store.rename(sid, text)

    def _save_group(self):
        sid = self.current_id()
        if sid and not self._loading:
            g = self.group.currentText().strip()
            s = self.store.get(sid)
            if s and s.group != g:
                self.store.update(sid, group=g)

    def _save_note(self):
        sid = self.current_id()
        if sid:
            s = self.store.get(sid)
            text = self.note.toPlainText().strip()
            if s and s.note != text:
                self.store.update(sid, note=text)

    def _save_colour(self, colour):
        sid = self.current_id()
        if sid:
            self.store.update(sid, color=colour)

    def _save_layers(self, on):
        sid = self.current_id()
        if sid and not self._loading:
            self.store.remember_layers(sid, on)

    def _save_scope(self):
        sid = self.current_id()
        if sid and not self._loading:
            self.store.move_to_scope(sid, self.scope.currentData())

    def _reordered(self, order, regroup):
        for sid, g in regroup.items():
            self.store.update(sid, group=g)
        self.store.reorder(order)

    def move_selected(self, step):
        ids = self.list.selected_ids()
        if not ids:
            return
        stops = self.store.stops()
        order = [s.id for s in stops]
        idx = sorted(order.index(i) for i in ids if i in order)
        if not idx:
            return
        if step < 0 and idx[0] == 0 or step > 0 and idx[-1] == len(order) - 1:
            return
        rng = idx if step < 0 else list(reversed(idx))
        for i in rng:
            order[i], order[i + step] = order[i + step], order[i]
        self.store.reorder(order)
        self.refresh()
        self.list.select_ids(ids)

    # ---- actions ------------------------------------------------------------------------------------------------
    def new_stop(self):
        from .actions import new_stop

        s = new_stop(self.plugin)
        if s is not None:
            self.refresh()
            self.list.select_ids([s.id])
            for it in self.list.stop_items():
                if it.data(ROLE_STOP).id == s.id:
                    self.list.scrollToItem(it)

    def go(self, sid):
        if sid:
            from .actions import go_to

            go_to(self.plugin, sid)

    def play(self, ids=None):
        from .actions import play_tour

        play_tour(self.plugin, ids or None)

    def update_view(self):
        sid = self.current_id()
        if sid:
            self.store.update_view(sid, self.plugin.iface.mapCanvas())

    def update_picture(self):
        sid = self.current_id()
        if not sid:
            return
        s = self.store.get(sid)
        canvas = self.plugin.iface.mapCanvas()
        self.store.request_thumb(sid, canvas.mapSettings(), s.frame)

    def delete_selected(self):
        ids = self.list.selected_ids()
        if not ids:
            return
        names = [self.store.get(i).name for i in ids if self.store.get(i)]
        snaps = self.store.delete(ids)
        if not snaps:
            return
        self._undo = snaps
        text = tr("Deleted «{name}».").format(name=names[0]) if len(snaps) == 1 else \
            tr("Deleted {n} stops.").format(n=native_digits(str(len(snaps))))
        self.undo_text.setText(text)
        self.undo_bar.show()
        self._undo_timer.start(12000)

    def undo(self):
        if not self._undo:
            return
        snaps, self._undo = self._undo, None
        self.store.restore(snaps)
        self.undo_bar.hide()
        self.refresh()
        self.list.select_ids([s["bookmark"].id() for s in snaps])

    def _send(self, sid):
        from .actions import send_to_phone

        send_to_phone(self.plugin, sid)

    def _export(self, ids):
        from .actions import export_dialog

        export_dialog(self.plugin, ids or None)

    def _import(self):
        from .actions import import_dialog

        import_dialog(self.plugin)

    def _options(self):
        from .actions import options_dialog

        options_dialog(self.plugin)
        self.list.jump_hint = __import__(f"{__package__}.actions", fromlist=["x"]).jump_hint()
        self.refresh()

    def _context(self, pos):
        it = self.list.itemAt(pos)
        if it is None or it.data(ROLE_KIND) != "stop":
            return
        if not it.isSelected():
            self.list.clearSelection()
            it.setSelected(True)
        ids = self.list.selected_ids()
        sid = it.data(ROLE_STOP).id
        t = self._tokens()
        m = QMenu(self)

        def add(icon, text, fn, enabled=True):
            a = m.addAction(icons.icon(icon, t["icon"], t["icon_disabled"]) if icon else QIcon(), text)
            a.setEnabled(enabled)
            a.triggered.connect(lambda _=False: fn())
            return a

        one = len(ids) == 1
        add("focus-2", tr("Go"), lambda: self.go(sid), one)
        add("pencil", tr("Rename") + "\tF2", lambda: self.list.editItem(it), one)
        add("refresh", tr("Update to current view"), self.update_view, one)
        add("photo", tr("New picture"), self.update_picture, one)
        add("stops/copy-plus", tr("Duplicate"), lambda: self.store.duplicate(sid), one)
        add("device-mobile-pin", tr("Send to a phone…"), lambda: self._send(sid), one)
        m.addSeparator()
        gm = m.addMenu(icons.icon("folder", t["icon"]), tr("Put in group"))
        for g in self.store.groups():
            a = gm.addAction(g)
            a.triggered.connect(lambda _=False, g=g: self.store.set_group(ids, g))
        gm.addSeparator()
        gm.addAction(tr("New group…")).triggered.connect(lambda: self._new_group(ids))
        gm.addAction(tr("No group")).triggered.connect(lambda: self.store.set_group(ids, ""))
        cm = m.addMenu(icons.icon("palette", t["icon"]), tr("Colour"))
        for col in ("",) + COLOURS:
            a = cm.addAction(_swatch_icon(col), tr("No colour") if not col else col)
            a.triggered.connect(lambda _=False, c=col: [self.store.update(i, color=c) for i in ids])
        add("stops/arrow-up", tr("Move up") + "\tCtrl+↑", lambda: self.move_selected(-1))
        add("stops/arrow-down", tr("Move down") + "\tCtrl+↓", lambda: self.move_selected(1))
        m.addSeparator()
        add("stops/player-play", tr("Tour of these"), lambda: self.play(ids), len(ids) > 1)
        add("trash", tr("Delete") + "\tDel", self.delete_selected)
        m.exec(self.list.viewport().mapToGlobal(pos))

    def _new_group(self, ids):
        name, ok = QInputDialog.getText(self, tr("New group"), tr("Group name"))
        if ok and name.strip():
            self.store.set_group(ids, name.strip())

    def shutdown(self):
        try:
            self.store.changed.disconnect(self._refresh_timer.start)
        except (TypeError, RuntimeError):
            pass


_PANES = {}


def open_pane(plugin, show=True):
    d = _PANES.get(id(plugin))
    if d is None:
        d = StopsPane(plugin)
        from ..commands import tab_beside

        tab_beside(plugin.iface.mainWindow(), d, getattr(plugin, "toolbox", None))
        _PANES[id(plugin)] = d
        theme = getattr(plugin, "theme", None)
        if theme is not None and hasattr(theme, "on_change"):
            theme.on_change(lambda: d.refresh_theme() if _PANES.get(id(plugin)) is d else None)
    if show:
        if d.isFloating():
            from ..commands import tab_beside

            tab_beside(plugin.iface.mainWindow(), d, getattr(plugin, "toolbox", None))
        d.show()
        d.raise_()
    return d


def close_pane(plugin):
    d = _PANES.pop(id(plugin), None)
    if d is not None:
        d.shutdown()
        plugin.iface.mainWindow().removeDockWidget(d)
        d.deleteLater()


def shutdown(plugin):
    close_pane(plugin)
