"""The Biruni ribbon: tabbed command bar with grouped large/small buttons,
an integrated search (the QGIS locator), and quick appearance controls."""
import json

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QEvent, QPoint, QSize, Qt, QTimer
from qgis.PyQt.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QMenu, QScrollArea, QSizePolicy,
                                 QStackedWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)

from . import icons, workbench
from .commands import CATALOG
from .i18n import tr

LARGE_ICON = 28
SMALL_ICON = 16
USES_KEY = "biruni/ribbon/uses"

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
    color: {t['text']}; background: transparent;
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
QFrame#RibbonFoldPanel {{ background: {t['ribbon']}; border: 1px solid {t['border']}; border-radius: 8px; }}
QFrame#RibbonFoldPanel QWidget {{ background: {t['ribbon']}; }}
QToolButton#RibbonIcon {{ border: none; border-radius: 5px; padding: 4px; background: transparent; }}
QToolButton#RibbonIcon:hover {{ background: {t['hover']}; }}
QToolButton#RibbonIcon::menu-indicator {{ image: none; width: 0; }}
#RibbonSearch QLineEdit {{
    border: 1px solid {t['border']}; border-radius: 6px; padding: 4px 8px;
    background: {t['base']}; color: {t['text']}; min-height: 18px;
}}
#RibbonSearch QLineEdit:focus {{ border: 1px solid {t['accent']}; }}
QToolButton#RibbonTab:focus, QToolButton#RibbonLarge:focus, QToolButton#RibbonSmall:focus,
QToolButton#RibbonIcon:focus, QToolButton#RibbonFile:focus {{ border: 1px solid {t['accent']}; }}
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
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # Tab / Shift+Tab walk the page, Space or Enter runs
        # never narrower than the label: a page wider than the window scrolls (_PageScroller) instead of
        # squeezing the buttons until their names are cut ("Saved Sel...ons")
        if large:
            self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Preferred)
        else:
            self.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
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

    def sizeHint(self):  # noqa: N802 - at least 50 px wide, and never narrower than the label
        # (a min-width in the style sheet would let the layout squeeze the label: "Saved Sel...ons")
        s = super().sizeHint()
        if getattr(self, "large", False):
            s.setWidth(max(50, s.width()))
        return s

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()

    def focusInEvent(self, event):  # noqa: N802 - a button reached with Tab is scrolled into view
        super().focusInEvent(event)
        w = self.parentWidget()
        while w is not None and not isinstance(w, _PageScroller):
            w = w.parentWidget()
        if w is not None:
            w.ensureWidgetVisible(self, 24, 0)

    def keyPressEvent(self, event):  # noqa: N802 - Enter runs the button like Space
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup and self.menu() is not None:
                self.showMenu()
            else:
                self.click()
            return
        super().keyPressEvent(event)

    def _dock_visibility(self, _visible):
        self.sync()

    def _clicked(self):
        self.ribbon.note_use(self.cmd.id)
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
    """A titled group of buttons. It is put on its page with its first button, so a group whose buttons the
    workspace hides all (workbench.py) is never shown.

    When the page is wider than the window the group folds: it becomes one button with the group's name, and
    the button opens the whole group in a panel under it (_PageScroller.refit folds the groups used least
    first). Nothing is ever out of sight behind a scroll arrow."""

    def __init__(self, ribbon, title, page=None, tab=""):
        super().__init__()
        self.ribbon = ribbon
        self.page, self.tab, self.title = page, tab, title
        self._placed = page is None
        self.folded = False
        self.fold_btn = None
        self._panel = None
        self._panel_links = []
        self.setObjectName("RibbonGroup")
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 2)
        v.setSpacing(0)
        self.body = QWidget()
        bv = QVBoxLayout(self.body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(0)
        self.row = QHBoxLayout()
        self.row.setSpacing(1)
        bv.addLayout(self.row, 1)
        cap = QLabel(tr(title))
        cap.setObjectName("RibbonCaption")
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = cap.font()
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        cap.setFont(f)
        bv.addWidget(cap)
        v.addWidget(self.body)

    # ---- folding ---------------------------------------------------------------------------------------------
    def uses(self):
        """How often this group's buttons were pressed."""
        return sum(self.ribbon.uses.get(b.cmd.id, 0) for b in self.body.findChildren(RibbonButton))

    def _fold_button(self):
        b = QToolButton(self)
        b.setObjectName("RibbonLarge")
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        b.setIconSize(QSize(LARGE_ICON, LARGE_ICON))
        b.setText(tr(self.title).replace("&", "&&") + " ▾")
        b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        b.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        b.clicked.connect(lambda _=False: self.open_panel())
        self.layout().addWidget(b)
        self.fold_btn = b
        self.refresh_icon(self.ribbon.plugin.theme.tokens())

    def refresh_icon(self, t):
        if self.fold_btn is None:
            return
        first = next(iter(self.body.findChildren(RibbonButton)), None)
        if first is not None:
            self.fold_btn.setIcon(icons.icon(first.cmd.icon, t["icon"], t["icon_disabled"], t["accent"]))
        names = [tr(b.cmd.label).replace("\n", " ") for b in self.body.findChildren(RibbonButton)]
        self.fold_btn.setToolTip(tr(self.title) + ": " + ", ".join(names))

    def set_folded(self, on):
        if on == self.folded:
            return
        self.folded = on
        if on and self.fold_btn is None:
            self._fold_button()
        if self._panel is not None:
            self._panel.close()
        self.body.setVisible(not on)
        if self.fold_btn is not None:
            self.fold_btn.setVisible(on)

    def open_panel(self):
        """The folded group's buttons in a panel under its button; it closes when one of them is used."""
        if not self.folded or self._panel is not None:
            return
        panel = QFrame(self.ribbon, Qt.WindowType.Popup)
        panel.setObjectName("RibbonFoldPanel")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(6, 6, 6, 4)
        lay.addWidget(self.body)
        self.body.show()
        panel.adjustSize()
        btn = self.fold_btn
        at = btn.mapToGlobal(QPoint(0, btn.height() + 2))
        x = at.x() + btn.width() - panel.width() if self.isRightToLeft() else at.x()
        screen = btn.screen().availableGeometry() if btn.screen() is not None else None
        if screen is not None:
            x = max(screen.left() + 4, min(x, screen.right() - panel.width() - 4))
        panel.move(x, at.y())
        self._panel = panel
        for b in self.body.findChildren(RibbonButton):
            # before the command runs (a dialog it opens must not find the panel still up)
            if b.menu() is not None:
                self._panel_links.append((b.menu().triggered, self._close_panel))
            if b.popupMode() != QToolButton.ToolButtonPopupMode.InstantPopup or b.menu() is None:
                self._panel_links.append((b.released, self._close_panel))
        for sig, slot in self._panel_links:
            sig.connect(slot)
        panel.installEventFilter(self)
        panel.show()
        first = next((b for b in self.body.findChildren(RibbonButton) if b.isEnabled()), None)
        if first is not None:
            first.setFocus()

    def _close_panel(self, *_a):
        if self._panel is not None:
            self._panel.close()

    def eventFilter(self, obj, ev):  # noqa: N802 - the panel went away: the buttons come home
        if obj is self._panel and ev.type() == QEvent.Type.Hide:
            panel, self._panel = self._panel, None
            for sig, slot in self._panel_links:
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self._panel_links = []
            try:
                self.layout().insertWidget(0, self.body)
                self.body.setVisible(not self.folded)
            except RuntimeError:   # the ribbon is being torn down
                pass
            panel.removeEventFilter(self)
            panel.deleteLater()
        return super().eventFilter(obj, ev)

    def _shown(self, cid):
        """The button is made: the command can run here and the workspace does not hide it."""
        return (self.ribbon.runner.available(CATALOG[cid])
                and not self.ribbon.hidden.button(self.tab, self.title, cid))

    def _place(self):
        if not self._placed:
            self._placed = True
            self.ribbon.place_group(self.page, self)

    def large(self, cid):
        if not self._shown(cid):
            return None
        self._place()
        b = RibbonButton(self.ribbon, CATALOG[cid], True)
        b.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.row.addWidget(b)
        self.ribbon.buttons.append(b)
        return b

    def smalls(self, *cids):
        cids = [cid for cid in cids if self._shown(cid)]
        if not cids:
            return
        self._place()
        col = QVBoxLayout()
        col.setSpacing(1)
        col.setContentsMargins(0, 0, 0, 0)
        for cid in cids:
            b = RibbonButton(self.ribbon, CATALOG[cid], False)
            col.addWidget(b)
            self.ribbon.buttons.append(b)
        col.addStretch(1)
        self.row.addLayout(col)

    def custom(self, widget):
        if self.ribbon.hidden.place(self.tab, self.title):
            return
        self._place()
        self.row.addWidget(widget)

    def is_empty(self):
        return self.row.count() == 0


class _PageScroller(QScrollArea):
    """A ribbon page that is wider than the window folds its groups until it fits (``refit``): a folded group
    is one button that opens the whole group. Only a window too narrow even for the folded groups scrolls
    sideways (mouse wheel, or the small arrows at its ends) - the page never forces the window to be as wide
    as it is, which pushed the window past the screen's edge and cut off the end of the ribbon."""

    STEP = 320

    def __init__(self, page, ribbon=None):
        super().__init__()
        self.ribbon = ribbon
        self._fitting = False
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self.refit)
        self.setObjectName("RibbonScroll")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setWidget(page)
        # the page shows the ribbon's own background (the theme), the arrows sit on it
        self.setStyleSheet("QScrollArea#RibbonScroll, QScrollArea#RibbonScroll > QWidget > QWidget"
                           "{background: transparent; border: none;}"
                           "QToolButton#RibbonScrollArrow{background: palette(window); border: none;"
                           " font-size: 18px; font-weight: bold;}")
        self.arrows = []
        for text in ("‹", "›"):   # the arrow at the left, the arrow at the right
            b = QToolButton(self)
            b.setObjectName("RibbonScrollArrow")
            b.setText(text)
            b.setLayoutDirection(Qt.LayoutDirection.LeftToRight)  # "‹" would be drawn mirrored in Arabic
            b.setAutoRaise(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setFixedWidth(18)
            b.clicked.connect(lambda _=False, side=len(self.arrows): self._step(self._towards(side)))
            self.arrows.append(b)
        self.horizontalScrollBar().rangeChanged.connect(lambda *_: self._place_arrows())
        self.horizontalScrollBar().valueChanged.connect(lambda *_: self._place_arrows())

    def _towards(self, side):
        """+1 (towards the end of the page) or -1 for the arrow on the left (0) or the right (1). In a
        right-to-left page the start is at the right, so the left arrow goes on towards the end."""
        rtl = self.isRightToLeft()
        return (1 if rtl else -1) if side == 0 else (-1 if rtl else 1)

    def _step(self, direction):
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() + direction * self.STEP)

    def wheelEvent(self, event):  # noqa: N802 - the mouse wheel moves the page sideways
        bar = self.horizontalScrollBar()
        if bar.maximum() > 0:
            delta = event.angleDelta().y() or event.angleDelta().x()
            bar.setValue(bar.value() - delta)
            event.accept()
        else:
            super().wheelEvent(event)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.refit()
        self._place_arrows()

    def showEvent(self, event):  # noqa: N802 - a page is measured when it is seen (a hidden one has no width)
        super().showEvent(event)
        self._fit_timer.start()

    def groups(self):
        lay = self.widget().layout()
        return [w for w in (lay.itemAt(i).widget() for i in range(lay.count())) if isinstance(w, RibbonGroup)]

    def request_refit(self):
        self._fit_timer.start()

    def refit(self):
        """Fold as many groups as it takes for the page to fit the window: the group used least first (of two
        used the same, the one nearer the end), and none when there is room for all."""
        if self._fitting or not self.isVisible():
            return
        room = self.viewport().width()
        groups = self.groups()
        if room <= 0 or not groups:
            return
        lay = self.widget().layout()

        def need():
            lay.invalidate()
            return lay.sizeHint().width()
        self._fitting = True
        self.setUpdatesEnabled(False)
        try:
            for g in groups:
                g.set_folded(False)
            order = sorted(range(len(groups)), key=lambda i: (groups[i].uses(), -i))
            for i in order:
                if need() <= room:
                    break
                g = groups[i]
                wide = g.sizeHint().width()
                g.set_folded(True)
                g.layout().invalidate()
                if g.sizeHint().width() > wide - 12:   # one large button: folding it gains nothing
                    g.set_folded(False)
            # folding a wide group can leave room that a narrow one, folded before it, can have back
            for i in reversed(order):
                g = groups[i]
                if g.folded:
                    g.set_folded(False)
                    g.layout().invalidate()
                    if need() > room:
                        g.set_folded(True)
                        g.layout().invalidate()
        finally:
            self.setUpdatesEnabled(True)
            self._fitting = False
        self._place_arrows()

    def _place_arrows(self):
        bar = self.horizontalScrollBar()
        left, right = self.arrows
        h = self.viewport().height()
        left.setGeometry(0, 0, 18, h)
        right.setGeometry(self.width() - 18, 0, 18, h)
        more = {1: bar.value() < bar.maximum(), -1: bar.value() > bar.minimum()}
        left.setVisible(more[self._towards(0)])
        right.setVisible(more[self._towards(1)])
        left.raise_()
        right.raise_()


class Ribbon(QWidget):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.runner = plugin.runner
        self.buttons = []
        self.header_icons = []
        self.pages = {}
        try:   # {command id: times pressed} - the groups used least fold first when the window is narrow
            self.uses = {str(k): int(v) for k, v in json.loads(QgsSettings().value(USES_KEY, "{}") or "{}").items()}
        except (ValueError, TypeError, AttributeError):
            self.uses = {}
        self.workbench = workbench.current()
        self.hidden = workbench.Hidden(self.workbench)
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
        self.search_holder.setMinimumWidth(220)
        h.addWidget(self.search_holder)

        self.workbench_btn = self._header_icon("layout-board", tr("Workspace"),
                                               getattr(plugin, "fill_workbench_menu", None))
        self.theme_btn = self._header_icon("sun-moon", tr("Theme"), plugin.fill_theme_menu)
        self.lang_btn = self._header_icon("language", tr("Language"), plugin.fill_language_menu)
        self.help_btn = self._header_icon("help-circle", tr("Help"), plugin.fill_help_menu)
        self.all_btn = self._header_icon("menu-2", tr("All Menus"), plugin.fill_all_menus)
        self.collapse_btn = self._header_icon("chevron-up", tr("Collapse the ribbon"), None)
        self.collapse_btn.clicked.connect(self.toggle_collapsed)
        for b in (self.workbench_btn, self.theme_btn, self.lang_btn, self.help_btn, self.all_btn,
                  self.collapse_btn):
            h.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
        self._workbench_tip()
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
        b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        b.setAccessibleName(tip)
        b._icon_name = icon_name
        if fill is not None:
            m = QMenu(b)
            m.aboutToShow.connect(lambda m=m, fill=fill: (m.clear(), fill(m)))
            b.setMenu(m)
            b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.header_icons.append(b)
        return b

    def _workbench_tip(self):
        tip = tr("Workspace: {name}").format(name=tr(workbench.WORKBENCHES[self.workbench].title))
        self.workbench_btn.setToolTip(tip)
        self.workbench_btn.setAccessibleName(tip)

    def use_workbench(self, name):
        """Hide what the workspace ``name`` hides from the next build (plugin.build_ribbon) on."""
        self.workbench = name if name in workbench.WORKBENCHES else workbench.DEFAULT
        self.hidden = workbench.Hidden(self.workbench)
        self._workbench_tip()

    def add_tab(self, key, title):
        """Create a tab, or empty and return it if it already exists (rebuild). The tab shows once a group
        is put on it, so a tab the workspace leaves empty is not shown."""
        if key in self.pages:
            self.clear_page(key)
            self.tab_button(key).setVisible(False)
            return self.pages[key]
        btn = QToolButton()
        btn.setObjectName("RibbonTab")
        btn.setText(tr(title))
        btn.setCheckable(True)
        btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # F10 comes here; ← / → change the tab
        btn.installEventFilter(self)
        idx = len(self._tab_keys)
        self.tabs.addButton(btn, idx)
        self.tab_bar.addWidget(btn)
        btn.setVisible(False)
        page = QWidget()
        lay = QHBoxLayout(page)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(4)
        lay.addStretch(1)
        self.body.addWidget(_PageScroller(page, self))
        self._tab_keys.append(key)
        self.pages[key] = page
        if idx == 0:
            btn.setChecked(True)
        return page

    def group(self, page, title):
        """A group for ``page``; it is put on the page with its first button (RibbonGroup._place)."""
        tab = next((k for k, p in self.pages.items() if p is page), "")
        return RibbonGroup(self, title, page, tab)

    def note_use(self, cid):
        self.uses[cid] = self.uses.get(cid, 0) + 1
        QgsSettings().setValue(USES_KEY, json.dumps(self.uses))

    def scroller(self, page):
        w = page.parentWidget()
        while w is not None and not isinstance(w, _PageScroller):
            w = w.parentWidget()
        return w

    def refit(self):
        """Fold or unfold the groups of the page that is shown, to fit the window."""
        for page in self.pages.values():
            sc = self.scroller(page)
            if sc is not None and sc.isVisible():
                sc.refit()

    def place_group(self, page, g):
        sc = self.scroller(page)
        if sc is not None:
            sc.request_refit()
        lay = page.layout()
        if g.tab:
            self.tab_button(g.tab).setVisible(True)
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
        if ev.type() == QEvent.Type.KeyPress and obj in self.tabs.buttons():
            return self._tab_key(obj, ev.key())
        return False

    def _tab_key(self, btn, key):
        """On a tab: ← / → (as seen) go to the next tab, Enter / Space / ↓ go into its page, Esc back to the
        map."""
        idx = self.tabs.id(btn)
        n = len(self._tab_keys)
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = 1 if (key == Qt.Key.Key_Right) != self.isRightToLeft() else -1
            nxt_idx = (idx + step) % n
            while self.tabs.button(nxt_idx).isHidden() and nxt_idx != idx:  # a tab the workspace hides
                nxt_idx = (nxt_idx + step) % n
            nxt = self.tabs.button(nxt_idx)
            nxt.setChecked(True)
            self._tab_clicked(nxt_idx)
            nxt.setFocus(Qt.FocusReason.TabFocusReason)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space, Qt.Key.Key_Down):
            btn.setChecked(True)
            self._tab_clicked(idx)
            first = next((b for b in self.pages[self._tab_keys[idx]].findChildren(RibbonButton)
                          if b.isVisible() and b.isEnabled()), None)
            if first is not None:
                first.setFocus(Qt.FocusReason.TabFocusReason)
            return True
        if key == Qt.Key.Key_Escape:
            self.leave_keyboard()
            return True
        return False

    def focus_tabs(self):
        """F10: the keyboard goes to the ribbon's current tab."""
        btn = self.tabs.checkedButton() or self.tabs.button(0)
        if btn is not None:
            if self._collapsed:
                self.set_collapsed(False)
            btn.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def leave_keyboard(self):
        canvas = getattr(getattr(self.plugin, "iface", None), "mapCanvas", lambda: None)()
        if canvas is not None:
            canvas.setFocus()

    def keyPressEvent(self, event):  # noqa: N802 - Esc inside a page goes back to the map
        if event.key() == Qt.Key.Key_Escape:
            self.leave_keyboard()
            return
        super().keyPressEvent(event)

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

    def keep_shown_tab(self):
        """After a rebuild: when the open tab is one the workspace hides, open the first tab shown."""
        cur = self.tabs.checkedButton()
        if cur is not None and not cur.isHidden():
            return
        key = next((k for k in self._tab_keys if not self.tab_button(k).isHidden()), None)
        if key is not None:
            self.select_tab(key)

    def fit_height(self):
        """The body as tall as the tallest page needs (fonts and themes change it), at least 100 px."""
        need = 100
        for page in self.pages.values():
            need = max(need, page.sizeHint().height())
        if self.body.height() != need or self.body.minimumHeight() != need:
            self.body.setFixedHeight(need)

    def refresh_theme(self):
        t = self.plugin.theme.tokens()
        self.setStyleSheet(ribbon_qss(t))
        for b in self.buttons:
            b.refresh_icon(t)
        for g in self.findChildren(RibbonGroup):
            g.refresh_icon(t)
        for b in self.header_icons:
            b.setIcon(icons.icon(b._icon_name, t["icon"]))
        self.ensurePolished()
        self.fit_height()
        self.refit()


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
