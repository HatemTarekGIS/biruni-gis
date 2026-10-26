"""Window layout: the modern (ribbon) interface vs. the classic QGIS menus and
toolbars. Switching never destroys QGIS UI - it only hides/shows it - so every
QGIS feature stays reachable (All Menus button, search, shortcuts)."""
from qgis.core import QgsSettings
from qgis.gui import QgsFloatingWidget, QgsStatusBar
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEvent, QObject, Qt, QTimer
from qgis.PyQt.QtGui import QAction
from qgis.PyQt.QtWidgets import QLineEdit, QMenu, QToolBar, QWidget

from .i18n import is_rtl, tr
from .ribbon import Ribbon, RibbonBar

CLASSIC_KEY = "biruni/classic"
CLASSIC_TOOLBARS_KEY = "biruni/classic-toolbars"
DEFAULT_CLASSIC_TOOLBARS = [
    "mFileToolBar", "mDigitizeToolBar", "mMapNavToolBar", "mAttributesToolBar", "mPluginToolBar",
    "mHelpToolBar", "mLabelToolBar", "mVectorToolBar", "mWebToolBar", "mDataSourceManagerToolBar",
    "mSelectionToolBar",
]


class Shell(QObject):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.iface = plugin.iface
        self.mw = self.iface.mainWindow()
        self.ribbon = None
        self.bar = None
        self._bound_actions = []
        self._classic_action = None
        self._locator_placeholder = None

    # ---- lifecycle -----------------------------------------------------
    def install(self):
        self.ribbon = Ribbon(self.plugin)
        self.plugin.build_ribbon(self.ribbon)
        self.bar = RibbonBar(self.ribbon, self.mw)
        first = next((t for t in self._main_toolbars() if t is not self.bar), None)
        if first is not None:
            self.mw.insertToolBar(first, self.bar)
            self.mw.insertToolBarBreak(first)
        else:
            self.mw.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.bar)
        self._install_view_menu_toggle()
        self.mw.installEventFilter(self)
        self.apply()

    def unload(self):
        self.mw.removeEventFilter(self)
        self._restore_classic()
        for a in self._bound_actions:
            self.mw.removeAction(a)
        self._bound_actions = []
        if self._classic_action is not None:
            for m in self.mw.findChildren(QMenu, "mViewMenu"):
                m.removeAction(self._classic_action)
        if self.bar is not None:
            self.mw.removeToolBar(self.bar)
            self.bar.deleteLater()

    # ---- mode ----------------------------------------------------------
    def is_classic(self):
        return QgsSettings().value(CLASSIC_KEY, False, type=bool)

    def set_classic(self, classic):
        QgsSettings().setValue(CLASSIC_KEY, bool(classic))
        self.apply()

    def apply(self):
        if self.is_classic():
            self._restore_classic()
        else:
            self._apply_modern()
        if self._classic_action is not None:
            self._classic_action.blockSignals(True)
            self._classic_action.setChecked(not self.is_classic())
            self._classic_action.blockSignals(False)

    def _main_toolbars(self):
        return [t for t in self.mw.findChildren(QToolBar) if t.parent() is self.mw]

    def _apply_modern(self):
        self.mw.menuBar().setVisible(False)
        hidden = set(QgsSettings().value(CLASSIC_TOOLBARS_KEY, [], type=list) or [])
        for tb in self._main_toolbars():
            if tb is self.bar:
                continue
            if tb.isVisible() and tb.objectName():
                hidden.add(tb.objectName())
            tb.setVisible(False)
        if hidden:
            QgsSettings().setValue(CLASSIC_TOOLBARS_KEY, sorted(hidden))
        self.bar.setVisible(True)
        self._bind_shortcuts()
        self._move_locator(into_ribbon=True)

    def _restore_classic(self):
        self.mw.menuBar().setVisible(True)
        names = QgsSettings().value(CLASSIC_TOOLBARS_KEY, [], type=list) or DEFAULT_CLASSIC_TOOLBARS
        for tb in self._main_toolbars():
            if tb.objectName() in names:
                tb.setVisible(True)
        if self.bar is not None:
            self.bar.setVisible(False)
        self._move_locator(into_ribbon=False)

    # ---- keyboard shortcuts --------------------------------------------
    def _bind_shortcuts(self):
        """Actions only reachable through the (hidden) menu bar or toolbars are
        also attached to the main window so their shortcuts keep working."""
        existing = set(self.mw.actions())
        for a in self._all_ui_actions():
            if a in existing or a.shortcut().isEmpty():
                continue
            self.mw.addAction(a)
            existing.add(a)
            self._bound_actions.append(a)

    def _all_ui_actions(self):
        seen, out = set(), []

        def walk_menu(menu):
            for a in menu.actions():
                if id(a) in seen:
                    continue
                seen.add(id(a))
                if a.menu() is not None:
                    walk_menu(a.menu())
                elif not a.isSeparator():
                    out.append(a)

        walk_menu(self.mw.menuBar())
        for tb in self._main_toolbars():
            for a in tb.actions():
                if id(a) not in seen and not a.isSeparator():
                    seen.add(id(a))
                    out.append(a)
        return out

    # ---- search (QGIS locator) -----------------------------------------
    def _locator(self):
        # QGIS 4 does not expose the locator's concrete type to Python, so look
        # it up by its object name.
        return self.mw.findChild(QWidget, "LocatorWidget")

    def _move_locator(self, into_ribbon):
        loc = self._locator()
        if loc is None or self.ribbon is None:
            return
        holder = self.ribbon.search_holder
        edit = loc.findChild(QLineEdit)
        if into_ribbon and loc.parentWidget() is not holder:
            holder.layout().addWidget(loc)
            loc.show()
        elif not into_ribbon and loc.parentWidget() is holder:
            holder.layout().removeWidget(loc)
            self.iface.statusBarIface().addPermanentWidget(loc, 0, QgsStatusBar.Anchor.AnchorLeft)
            loc.show()
        if edit is not None:
            if self._locator_placeholder is None:
                self._locator_placeholder = edit.placeholderText()
            edit.setPlaceholderText(tr("Search tools, layers and places…  (Ctrl+K)") if into_ribbon
                                    else self._locator_placeholder)
        self._anchor_locator_results(loc, below=into_ribbon)

    def _anchor_locator_results(self, loc, below):
        A = QgsFloatingWidget.AnchorPoint
        for w in self.mw.findChildren(QWidget):
            if w.metaObject().className() != "QgsLocatorResultsView" or w.parentWidget() is None:
                continue
            fw = sip.cast(w.parentWidget(), QgsFloatingWidget)
            if below:
                fw.setAnchorPoint(A.TopRight if is_rtl() else A.TopLeft)
                fw.setAnchorWidgetPoint(A.BottomRight if is_rtl() else A.BottomLeft)
            else:
                fw.setAnchorPoint(A.BottomLeft)
                fw.setAnchorWidgetPoint(A.TopLeft)

    # ---- plugin toolbars that appear later -----------------------------
    def eventFilter(self, obj, ev):
        if obj is self.mw and ev.type() == QEvent.Type.ChildAdded:
            child = ev.child()
            if isinstance(child, QToolBar) and child is not self.bar:
                QTimer.singleShot(0, lambda c=child: self._late_toolbar(c))
        return False

    def _late_toolbar(self, tb):
        try:
            if tb.parent() is self.mw and not self.is_classic():
                tb.setVisible(False)
        except RuntimeError:  # deleted meanwhile
            pass

    # ---- way back from classic mode ------------------------------------
    def _install_view_menu_toggle(self):
        view = self.mw.findChild(QMenu, "mViewMenu")
        if view is None:
            return
        self._classic_action = QAction(tr("Biruni Ribbon Interface"), self.mw)
        self._classic_action.setCheckable(True)
        self._classic_action.toggled.connect(lambda on: self.set_classic(not on))
        view.addSeparator()
        view.addAction(self._classic_action)
