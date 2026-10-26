"""The Biruni ribbon: tabbed command bar with grouped large/small buttons,
an integrated search (the QGIS locator), and quick appearance controls."""
from qgis.PyQt.QtCore import QEvent, QSize, Qt
from qgis.PyQt.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QMenu, QSizePolicy,
                                 QStackedWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)

from . import icons
from .commands import CATALOG
from .i18n import tr

LARGE_ICON = 28
SMALL_ICON = 16

# Toolbars that belong to QGIS itself; anything else is a plugin toolbar and is
# surfaced on the "Plugins" ribbon tab.
CORE_TOOLBARS = {
    "mFileToolBar", "mLayerToolBar", "mDigitizeToolBar", "mAdvancedDigitizeToolBar",
    "mMapNavToolBar", "mAttributesToolBar", "mHelpToolBar", "mRasterToolBar",
    "mLabelToolBar", "mSnappingToolBar", "mDataSourceManagerToolBar", "mShapeDigitizeToolBar",
    "mSelectionToolBar", "mMeshToolBar", "mAnnotationsToolBar", "mGpsToolBar", "BiruniRibbonBar",
}
PLUGIN_HOST_TOOLBARS = ("mPluginToolBar", "mVectorToolBar", "mRasterToolBar", "mDatabaseToolBar", "mWebToolBar")


def ribbon_qss(t):
    return f"""
#BiruniRibbon, #RibbonHeader {{ background: {t['ribbon']}; }}
#RibbonBody {{ background: {t['ribbon']}; border-bottom: 1px solid {t['border']}; }}
#RibbonBody > QWidget {{ background: {t['ribbon']}; }}
QToolButton#RibbonTab {{
    border: none; border-bottom: 2px solid transparent; background: transparent;
    padding: 5px 11px 6px 11px; color: {t['text_dim']};
}}
QToolButton#RibbonTab:hover {{ color: {t['text']}; }}
QToolButton#RibbonTab:checked {{ color: {t['accent']}; border-bottom: 2px solid {t['accent']}; font-weight: 600; }}
QToolButton#RibbonFile {{
    background: {t['accent']}; color: {t['accent_text']}; border: none; border-radius: 5px;
    padding: 4px 16px; font-weight: 600; margin: 0 8px 2px 0;
}}
QToolButton#RibbonFile:hover {{ background: {t['accent_hover']}; }}
QToolButton#RibbonFile::menu-indicator {{ image: none; width: 0; }}
QToolButton#RibbonLarge {{
    border: 1px solid transparent; border-radius: 6px; padding: 3px 5px 2px 5px;
    color: {t['text']}; background: transparent; min-width: 50px;
}}
QToolButton#RibbonSmall {{
    border: 1px solid transparent; border-radius: 5px; padding: 2px 8px 2px 4px;
    color: {t['text']}; background: transparent; text-align: left;
}}
QToolButton#RibbonLarge:hover, QToolButton#RibbonSmall:hover {{ background: {t['hover']}; }}
QToolButton#RibbonLarge:pressed, QToolButton#RibbonSmall:pressed {{ background: {t['pressed']}; }}
QToolButton#RibbonLarge:checked, QToolButton#RibbonSmall:checked {{
    background: {t['accent_soft']}; border-color: {t['accent_soft']}; color: {t['accent']};
}}
QToolButton#RibbonLarge:disabled, QToolButton#RibbonSmall:disabled {{ color: {t['text_disabled']}; }}
QToolButton#RibbonLarge::menu-indicator, QToolButton#RibbonSmall::menu-indicator {{
    image: none; width: 0;
}}
QToolButton#RibbonLarge::menu-button {{ border: none; width: 12px; }}
QLabel#RibbonCaption {{ color: {t['caption']}; padding-top: 1px; }}
QFrame#RibbonSep {{ background: {t['border']}; border: none; }}
QToolButton#RibbonIcon {{ border: none; border-radius: 5px; padding: 4px; background: transparent; }}
QToolButton#RibbonIcon:hover {{ background: {t['hover']}; }}
QToolButton#RibbonIcon::menu-indicator {{ image: none; width: 0; }}
#RibbonSearch QLineEdit {{
    border: 1px solid {t['border']}; border-radius: 6px; padding: 4px 8px;
    background: {t['base']}; color: {t['text']}; min-height: 18px;
}}
#RibbonSearch QLineEdit:focus {{ border: 1px solid {t['accent']}; }}
"""


class RibbonButton(QToolButton):
    def __init__(self, ribbon, cmd, large):
        super().__init__()
        self.ribbon, self.cmd, self.large = ribbon, cmd, large
        runner = ribbon.runner
        self.setObjectName("RibbonLarge" if large else "RibbonSmall")
        text = tr(cmd.label).replace("&", "&&")  # a single "&" would become a mnemonic
        self.setText(text if large else text.replace("\n", " "))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon if large
                                else Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        size = LARGE_ICON if large else SMALL_ICON
        self.setIconSize(QSize(size, size))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if not large:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._action = runner.qaction(cmd.action) if cmd.action else None
        self._dock = runner.dock(cmd.dock) if cmd.dock else None
        if self._action is not None:
            self._action.changed.connect(self.sync)
        if self._dock is not None:
            self._dock.visibilityChanged.connect(self._dock_visibility)
            self.setCheckable(True)
        if cmd.menu:
            self.setMenu(ribbon.build_menu(cmd))
            if cmd.action or cmd.alg or cmd.run:
                self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            else:
                self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.clicked.connect(self._clicked)
        self.sync()

    def _dock_visibility(self, _visible):
        self.sync()

    def _clicked(self):
        if self.cmd.menu and not (self.cmd.action or self.cmd.alg or self.cmd.run):
            return
        self.ribbon.runner.execute(self.cmd)
        self.sync()

    def sync(self):
        a = self._action
        tip = tr(self.cmd.label).replace("\n", " ")
        if a is not None:
            self.setEnabled(a.isEnabled())
            self.setCheckable(a.isCheckable())
            if a.isCheckable():
                self.setChecked(a.isChecked())
            if a.shortcut().toString():
                tip += f"  ({a.shortcut().toString()})"
        elif self._dock is not None:
            self.setChecked(self._dock.isVisible())
        self.setToolTip(tip)

    def refresh_icon(self, t):
        self.setIcon(icons.icon(self.cmd.icon, t["icon"], t["icon_disabled"], t["accent"]))


class RibbonGroup(QFrame):
    def __init__(self, ribbon, title):
        super().__init__()
        self.ribbon = ribbon
        self.setObjectName("RibbonGroup")
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 2)
        v.setSpacing(0)
        self.row = QHBoxLayout()
        self.row.setSpacing(1)
        v.addLayout(self.row, 1)
        cap = QLabel(tr(title))
        cap.setObjectName("RibbonCaption")
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = cap.font()
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        cap.setFont(f)
        v.addWidget(cap)

    def large(self, cid):
        cmd = CATALOG[cid]
        if not self.ribbon.runner.available(cmd):
            return None
        b = RibbonButton(self.ribbon, cmd, True)
        b.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.row.addWidget(b)
        self.ribbon.buttons.append(b)
        return b

    def smalls(self, *cids):
        col = QVBoxLayout()
        col.setSpacing(1)
        col.setContentsMargins(0, 0, 0, 0)
        n = 0
        for cid in cids:
            cmd = CATALOG[cid]
            if not self.ribbon.runner.available(cmd):
                continue
            b = RibbonButton(self.ribbon, cmd, False)
            col.addWidget(b)
            self.ribbon.buttons.append(b)
            n += 1
        if n:
            col.addStretch(1)
            self.row.addLayout(col)

    def custom(self, widget):
        self.row.addWidget(widget)

    def is_empty(self):
        return self.row.count() == 0


class Ribbon(QWidget):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.runner = plugin.runner
        self.buttons = []
        self.header_icons = []
        self.pages = {}
        self.setObjectName("BiruniRibbon")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.header = QWidget()
        self.header.setObjectName("RibbonHeader")
        h = QHBoxLayout(self.header)
        h.setContentsMargins(8, 6, 8, 0)
        h.setSpacing(2)

        self.file_btn = QToolButton()
        self.file_btn.setObjectName("RibbonFile")
        self.file_btn.setText(tr("File"))
        self.file_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.file_btn.setMenu(plugin.build_file_menu())
        h.addWidget(self.file_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self.tab_bar = QHBoxLayout()
        self.tab_bar.setSpacing(0)
        h.addLayout(self.tab_bar)
        h.addStretch(1)

        self.search_holder = QWidget()
        self.search_holder.setObjectName("RibbonSearch")
        sh = QHBoxLayout(self.search_holder)
        sh.setContentsMargins(0, 0, 6, 2)
        self.search_holder.setMinimumWidth(320)
        h.addWidget(self.search_holder)

        self.theme_btn = self._header_icon("sun-moon", tr("Theme"), plugin.fill_theme_menu)
        self.lang_btn = self._header_icon("language", tr("Language"), plugin.fill_language_menu)
        self.help_btn = self._header_icon("help-circle", tr("Help"), plugin.fill_help_menu)
        self.all_btn = self._header_icon("menu-2", tr("All Menus"), plugin.fill_all_menus)
        self.collapse_btn = self._header_icon("chevron-up", tr("Collapse the ribbon"), None)
        self.collapse_btn.clicked.connect(self.toggle_collapsed)
        for b in (self.theme_btn, self.lang_btn, self.help_btn, self.all_btn, self.collapse_btn):
            h.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(self.header)

        self.body = QStackedWidget()
        self.body.setObjectName("RibbonBody")
        self.body.setFixedHeight(100)
        root.addWidget(self.body)

        self.tabs = QButtonGroup(self)
        self.tabs.setExclusive(True)
        self.tabs.idClicked.connect(self._tab_clicked)
        self._tab_keys = []
        self._collapsed = False

    # ---- construction --------------------------------------------------
    def _header_icon(self, icon_name, tip, fill):
        b = QToolButton()
        b.setObjectName("RibbonIcon")
        b.setToolTip(tip)
        b.setIconSize(QSize(18, 18))
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        b._icon_name = icon_name
        if fill is not None:
            m = QMenu(b)
            m.aboutToShow.connect(lambda m=m, fill=fill: (m.clear(), fill(m)))
            b.setMenu(m)
            b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.header_icons.append(b)
        return b

    def add_tab(self, key, title):
        """Create a tab, or empty and return it if it already exists (rebuild)."""
        if key in self.pages:
            self.clear_page(key)
            return self.pages[key]
        btn = QToolButton()
        btn.setObjectName("RibbonTab")
        btn.setText(tr(title))
        btn.setCheckable(True)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.installEventFilter(self)
        idx = len(self._tab_keys)
        self.tabs.addButton(btn, idx)
        self.tab_bar.addWidget(btn)
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(4)
        lay.addStretch(1)
        self.body.addWidget(page)
        self._tab_keys.append(key)
        self.pages[key] = page
        if idx == 0:
            btn.setChecked(True)
        return page

    def group(self, page, title):
        lay = page.layout()
        g = RibbonGroup(self, title)
        pos = lay.count() - 1  # before the trailing stretch
        if pos > 0:
            sep = QFrame()
            sep.setObjectName("RibbonSep")
            sep.setFixedWidth(1)
            lay.insertWidget(pos, sep)
            pos += 1
        lay.insertWidget(pos, g)
        return g

    def tab_button(self, key):
        return self.tabs.button(self._tab_keys.index(key))

    def clear_page(self, key):
        page = self.pages[key]
        lay = page.layout()
        while lay.count() > 1:
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                for b in w.findChildren(RibbonButton):
                    if b in self.buttons:
                        self.buttons.remove(b)
                w.deleteLater()

    def build_menu(self, cmd):
        """Dropdown for a command; filled on every show so icons follow the theme."""
        menu = QMenu(self)
        if callable(cmd.menu):
            menu.aboutToShow.connect(lambda m=menu, c=cmd: (m.clear(), c.menu(self.plugin, m)))
        else:
            menu.aboutToShow.connect(lambda m=menu, c=cmd: (m.clear(), self._fill_command_menu(m, c)))
        return menu

    def _fill_command_menu(self, menu, cmd):
        t = self.plugin.theme.tokens()
        for cid in cmd.menu:
            sub = CATALOG[cid]
            if not self.runner.available(sub):
                continue
            act = menu.addAction(icons.icon(sub.icon, t["icon"]), tr(sub.label).replace("\n", " "))
            act.triggered.connect(lambda _=False, s=sub: self.runner.execute(s))

    # ---- behaviour -----------------------------------------------------
    def _tab_clicked(self, idx):
        self.body.setCurrentIndex(idx)
        if self._collapsed:
            self.set_collapsed(False)
        self.plugin.on_tab_shown(self._tab_keys[idx])

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.MouseButtonDblClick and isinstance(obj, QToolButton):
            self.toggle_collapsed()
            return True
        return False

    def toggle_collapsed(self):
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed):
        self._collapsed = collapsed
        self.body.setVisible(not collapsed)
        self.collapse_btn._icon_name = "chevron-down" if collapsed else "chevron-up"
        self.collapse_btn.setToolTip(tr("Expand the ribbon") if collapsed else tr("Collapse the ribbon"))
        self.refresh_theme()

    def select_tab(self, key):
        idx = self._tab_keys.index(key)
        self.tabs.button(idx).setChecked(True)
        self._tab_clicked(idx)

    def refresh_theme(self):
        t = self.plugin.theme.tokens()
        self.setStyleSheet(ribbon_qss(t))
        for b in self.buttons:
            b.refresh_icon(t)
        for b in self.header_icons:
            b.setIcon(icons.icon(b._icon_name, t["icon"]))


class RibbonBar(QToolBar):
    """Hosts the ribbon in the QGIS main window (top toolbar area)."""

    def __init__(self, ribbon, parent):
        super().__init__(tr("Biruni Ribbon"), parent)
        self.setObjectName("BiruniRibbonBar")
        self.setMovable(False)
        self.setFloatable(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.setStyleSheet("QToolBar#BiruniRibbonBar { border: none; padding: 0; margin: 0; spacing: 0; }")
        self.toggleViewAction().setVisible(False)
        self.addWidget(ribbon)
