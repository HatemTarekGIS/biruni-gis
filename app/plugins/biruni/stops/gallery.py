"""The ribbon dropdown of Map Stops: a filter (All / This project / All my projects / a group) and a search box,
the gallery of pictures (click = fly there), then the actions. Built each time the menu opens."""
from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QMenu, QToolButton, QVBoxLayout, QWidget,
                                 QWidgetAction)

from .. import icons
from ..i18n import tr
from .model import PROJECT, USER, store_for
from .stoplist import ROLE_STOP, StopListWidget, tile_size

FILTER_KEY = "biruni/stops/filter"
COLUMNS = 3
LIST_W = 548  # three tiles of ~170 px: as wide as the actions below (no empty band)


def _tokens(plugin):
    theme = getattr(plugin, "theme", None)
    if theme is not None:
        return theme.tokens()
    from ..theme import TOKENS

    return TOKENS["light"]


def gallery_qss(t):
    return f"""
#StopsGallery {{ background: {t['menu']}; }}
#StopsGallery QLabel#GalTitle {{ font-weight: 600; font-size: 10.5pt; color: {t['text']}; padding: 0 2px; }}
#StopsGallery QLabel#GalEmpty {{ color: {t['text_dim']}; padding: 18px 10px; }}
#StopsGallery QToolButton#GalFilter {{
    border: 1px solid {t['border']}; border-radius: 6px; padding: 3px 8px; color: {t['text']};
    background: {t['base']};
}}
#StopsGallery QToolButton#GalFilter:hover {{ border-color: {t['accent']}; }}
#StopsGallery QToolButton#GalFilter::menu-indicator {{ image: none; width: 0; }}
#StopsGallery QLineEdit {{
    border: 1px solid {t['border']}; border-radius: 6px; padding: 3px 8px; background: {t['base']};
    color: {t['text']};
}}
#StopsGallery QLineEdit:focus {{ border-color: {t['accent']}; }}
#StopsGallery QListWidget {{ background: transparent; border: none; outline: none; }}
#StopsGallery QFrame#GalRule {{ background: {t['border']}; border: none; }}
"""


def filter_value():
    return QgsSettings().value(FILTER_KEY, "all") or "all"


def filtered(store, value, query=""):
    if value == "project":
        stops = store.stops(PROJECT)
    elif value == "user":
        stops = store.stops(USER)
    elif value.startswith("group:"):
        stops = store.stops(group=value[6:])
    else:
        stops = store.stops()
    if query:
        from .model import search

        stops = search(stops, query)
    return stops


def filter_label(value):
    if value == "project":
        return tr("This project")
    if value == "user":
        return tr("All my projects")
    if value.startswith("group:"):
        return value[6:] or tr("Not in a group")
    return tr("All")


class Gallery(QWidget):
    def __init__(self, plugin, menu=None, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.menu = menu
        self.store = store_for(plugin)
        self.value = filter_value()
        self.setObjectName("StopsGallery")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        t = _tokens(plugin)
        self.setStyleSheet(gallery_qss(t))
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 6)
        v.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel(tr("Map Stops"))
        title.setObjectName("GalTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.filter_btn = QToolButton()
        self.filter_btn.setObjectName("GalFilter")
        self.filter_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.filter_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.filter_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft
                                           if self.layoutDirection() == Qt.LayoutDirection.LeftToRight
                                           else Qt.LayoutDirection.LeftToRight)  # the arrow after the text
        self.filter_btn.setIcon(icons.icon("chevron-down", t["text_dim"]))
        self.filter_btn.setIconSize(QSize(12, 12))
        fm = QMenu(self.filter_btn)
        fm.aboutToShow.connect(lambda: (fm.clear(), self._fill_filter(fm)))
        self.filter_btn.setMenu(fm)
        head.addWidget(self.filter_btn)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search stops"))
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(170)
        self.search.textChanged.connect(self.refresh)
        self.search.returnPressed.connect(self._enter)
        head.addWidget(self.search)
        v.addLayout(head)
        from .actions import jump_hint

        self.list = StopListWidget(lambda: _tokens(plugin), grid=True, editable=False, single_click=True)
        self.list.jump_hint = jump_hint()
        self.list.activated_stop.connect(self._go)
        v.addWidget(self.list)
        self.empty = QLabel()
        self.empty.setObjectName("GalEmpty")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.empty)
        # at least three tiles wide; the menu stretches it to its own width (no empty band beside it)
        self.list.setMinimumWidth(LIST_W)
        self.setMinimumWidth(LIST_W + 20)
        self.refresh()
        QTimer.singleShot(0, self.search.setFocus)

    def _fill_filter(self, m):
        options = [("all", tr("All")), ("project", tr("This project")), ("user", tr("All my projects"))]
        for value, text in options:
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

    def set_filter(self, value):
        self.value = value
        QgsSettings().setValue(FILTER_KEY, value)
        self.refresh()

    def refresh(self):
        self.filter_btn.setText(filter_label(self.value))
        stops = filtered(self.store, self.value, self.search.text())
        from .actions import numbers

        self.list.populate(stops, show_groups=not self.value.startswith("group:"), numbers=numbers(self.store),
                           keep_selection=False)
        has = bool(stops)
        self.list.setVisible(has)
        self.empty.setVisible(not has)
        if not has:
            if self.search.text():
                self.empty.setText(tr("No stop matches «{q}».").format(q=self.search.text()))
            else:
                self.empty.setText(tr("No stops yet. Save what the map shows now with New stop (Ctrl+Shift+B) - "
                                      "it keeps the place, the scale and a picture."))
        # the height: whole rows, at most about two and a half (more scroll)
        n_tiles = len(stops)
        headers = len({s.group for s in stops}) if any(s.group for s in stops) and \
            not self.value.startswith("group:") else 0
        rows = sum(-(-len([s for s in stops if s.group == g]) // COLUMNS)
                   for g in {s.group for s in stops}) if headers else -(-n_tiles // COLUMNS)
        th = tile_size(LIST_W - 14, self.list.spacing()).height() + 2 * self.list.spacing()
        want = rows * th + headers * 34 + 8
        self.list.setFixedHeight(max(th + 10, min(want, int(2.55 * th) + 10)))
        self.adjustSize()

    def _enter(self):
        """Enter in the search box: go to the first stop found."""
        items = [i for i in self.list.stop_items() if not i.isHidden()]
        if items:
            self._go(items[0].data(ROLE_STOP).id)

    def _go(self, sid):
        if self.menu is not None:
            self.menu.close()
        from .actions import go_to

        go_to(self.plugin, sid)


def fill_menu(plugin, menu):
    t = _tokens(plugin)
    gal = Gallery(plugin, menu)
    wa = QWidgetAction(menu)
    wa.setDefaultWidget(gal)
    menu.addAction(wa)
    menu.addSeparator()
    from . import view_history
    from .keys import NEW_STOP_KEY

    def add(icon, text, fn, key="", enabled=True):
        # the key between left-to-right marks, or an Arabic menu shows "←+Alt"
        a = menu.addAction(icons.icon(icon, t["icon"], t["icon_disabled"]),
                           text + ("\t‎" + key + "‎" if key else ""))
        a.setEnabled(enabled)
        a.triggered.connect(lambda _=False: fn(plugin))
        return a

    from . import (export_stops, go_back, go_forward, import_stops, new_stop, open_manager, options, play_tour,
                   send_to_phone)

    add("map-pin-plus", tr("New stop - save this view"), new_stop, NEW_STOP_KEY)
    h = view_history(plugin)
    add("arrow-back", tr("Back to the previous view"), go_back, "Alt+←", h.can_go(-1))
    add("arrow-forward", tr("Forward to the next view"), go_forward, "Alt+→", h.can_go(1))
    menu.addSeparator()
    add("list-details", tr("Manage stops…"), open_manager)
    add("stops/player-play", tr("Play as a tour"), play_tour, enabled=bool(gal.store.stops()))
    add("device-mobile-pin", tr("Send this view to a phone…"), lambda p: send_to_phone(p, None))
    menu.addSeparator()
    add("file-export", tr("Export…"), export_stops, enabled=bool(gal.store.stops()))
    add("file-import", tr("Import…"), import_stops)
    add("settings", tr("Options…"), options)
    return gal
