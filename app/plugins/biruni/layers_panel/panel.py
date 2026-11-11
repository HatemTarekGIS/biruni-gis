"""Puts the additions into the Layers panel and takes them out again (teardown restores the stock panel)."""
from qgis.core import Qgis, QgsMessageLog, QgsProject
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtGui import QAction
from qgis.PyQt.QtWidgets import QDockWidget, QLabel, QToolBar

from ..i18n import tr
from .toolbar import OWN_PREFIX, ToolbarLook, icon_for, tip

_DEFAULT_ICON = "#33414C"


def _log(text):
    QgsMessageLog.logMessage(text, "Biruni", Qgis.MessageLevel.Warning)


class LayersPanel(QObject):
    def __init__(self, plugin):
        mw = plugin.iface.mainWindow()
        super().__init__(mw)
        self.plugin = plugin
        self.iface = plugin.iface
        self.dock = mw.findChild(QDockWidget, "Layers")
        self.view = self.iface.layerTreeView()
        if self.view is None:
            raise RuntimeError("no layer tree view")
        self.toolbar = self.dock.findChild(QToolBar) if self.dock is not None else None
        self.look = self.search = self.search_edit = self.no_match = self.empty = None
        self.health = self.origin = self.solo = self.alt_click = None
        self.own_actions = []
        self.arrange_snapshot = None
        self._parts = []  # (name, teardown) of what was installed
        for name, fn in (("toolbar icons", self._install_look), ("own buttons", self._install_actions),
                         ("search", self._install_search), ("empty state", self._install_empty),
                         ("health", self._install_health), ("origin", self._install_origin)):
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - each part is optional
                _log(f"Layers panel: {name} not installed ({e!r})")
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        self.conns = [(project.readProject, self._project_changed), (project.cleared, self._project_changed),
                      (root.addedChildren, self._tree_changed), (root.removedChildren, self._tree_changed)]
        for sig, slot in self.conns:
            sig.connect(slot)
        theme = getattr(plugin, "theme", None)
        if theme is not None:
            theme.on_change(self.refresh_theme)
        self.refresh_theme()
        self.refresh_empty()

    # ---- parts -------------------------------------------------------------------------------------------------
    def _icon_color(self):
        theme = getattr(self.plugin, "theme", None)
        try:
            return theme.tokens().get("icon", _DEFAULT_ICON) if theme is not None else _DEFAULT_ICON
        except Exception:  # noqa: BLE001
            return _DEFAULT_ICON

    def _install_look(self):
        if self.toolbar is not None:
            self.look = ToolbarLook(self.toolbar)

    def _install_actions(self):
        from .solo import AltClick, Solo

        self.solo = Solo(self)
        self.alt_click = AltClick(self.view, self.solo)
        self.view.viewport().installEventFilter(self.alt_click)
        parent = self.toolbar if self.toolbar is not None else self.iface.mainWindow()
        self.solo_action = QAction(tr("Solo peek"), parent)
        self.solo_action.setObjectName(OWN_PREFIX + "Solo")
        self.solo_action.setCheckable(True)
        self.solo_action.triggered.connect(lambda _=False: self.toggle_solo())
        self.solo.changed.connect(self.solo_action.setChecked)
        self.arrange_action = QAction(tr("Smart arrange"), parent)
        self.arrange_action.setObjectName(OWN_PREFIX + "Arrange")
        self.arrange_action.triggered.connect(lambda _=False: self.smart_arrange())
        self.undo_action = QAction(tr("Undo arrange"), parent)
        self.undo_action.setObjectName(OWN_PREFIX + "ArrangeUndo")
        self.undo_action.triggered.connect(lambda _=False: self.undo_arrange())
        self.undo_action.setVisible(False)
        self.own_actions = [self.solo_action, self.arrange_action, self.undo_action]
        if self.toolbar is not None:
            sep = self.toolbar.addSeparator()
            sep.setObjectName(OWN_PREFIX + "Separator")
            self.own_actions.insert(0, sep)
            for act in self.own_actions[1:]:
                self.toolbar.addAction(act)

    def _layout_around_view(self):
        parent = self.view.parentWidget()
        lay = parent.layout() if parent is not None else None
        if lay is None or lay.indexOf(self.view) < 0:
            return None, -1
        return lay, lay.indexOf(self.view)

    def _install_search(self):
        from .search import LayerSearch, SearchEdit

        lay, at = self._layout_around_view()
        if lay is None:
            return
        self.search_edit = SearchEdit(self.view)
        self.no_match = QLabel()
        self.no_match.setObjectName("biruniLayersNoMatch")
        self.no_match.setWordWrap(True)
        self.no_match.hide()
        lay.insertWidget(at, self.search_edit)
        lay.insertWidget(at + 1, self.no_match)
        self.search = LayerSearch(self.view, self.search_edit, self._search_result)

    def _search_result(self, text, any_match):
        if self.no_match is None:
            return
        self.no_match.setText(tr("No layer matches «{text}».").format(text=text) if text and not any_match else "")
        self.no_match.setVisible(bool(text) and not any_match)

    def _install_empty(self):
        from .empty_state import EmptyState

        lay, at = self._layout_around_view()
        if lay is None:
            return
        self.empty = EmptyState(self.add_data, self.open_vault)
        self.empty.hide()
        lay.insertWidget(at + 1, self.empty, 1)

    def _install_health(self):
        from .health import Health

        self.health = Health(self.plugin, self.view, self.iface.mapCanvas())

    def _install_origin(self):
        from .origin import Origin

        self.origin = Origin(self.view)

    # ---- behaviour ---------------------------------------------------------------------------------------------
    def refresh_theme(self):
        color = self._icon_color()
        if self.look is not None:
            self.look.apply(color)
        for key, attr in (("solo", "solo_action"), ("arrange", "arrange_action"), ("arrange_undo", "undo_action")):
            act = getattr(self, attr, None)
            if act is not None:
                act.setIcon(icon_for(key, color))
                act.setToolTip(tip(key))
        if self.empty is not None:
            theme = getattr(self.plugin, "theme", None)
            try:
                self.empty.refresh_theme(theme.tokens() if theme is not None else None)
            except Exception:  # noqa: BLE001
                self.empty.refresh_theme(None)
        if self.no_match is not None:
            self.no_match.setStyleSheet("color: #D97706; padding: 2px 4px;")

    def is_empty(self):
        return not QgsProject.instance().layerTreeRoot().children()

    def refresh_empty(self):
        if self.empty is None:
            return
        empty = self.is_empty()
        self.empty.setVisible(empty)
        self.view.setVisible(not empty)
        if self.search_edit is not None:
            self.search_edit.setVisible(not empty)
            if empty and self.search_edit.text():
                self.search_edit.clear()

    def _tree_changed(self, *_args):
        QTimer.singleShot(0, self._after_tree_change)

    def _after_tree_change(self):
        try:
            self.refresh_empty()
        except RuntimeError:  # torn down meanwhile
            pass

    def _project_changed(self, *_args):
        if self.solo is not None:
            self.solo.forget()
        self.arrange_snapshot = None
        if getattr(self, "undo_action", None) is not None:
            self.undo_action.setVisible(False)
        self._tree_changed()

    def _runner_execute(self, cid):
        runner = getattr(self.plugin, "runner", None)
        if runner is None:
            return False
        from ..commands import CATALOG

        runner.execute(CATALOG[cid])
        return True

    def add_data(self):
        return self._runner_execute("add_data")

    def open_vault(self):
        return self._runner_execute("vault")

    def focus_search(self):
        if self.search_edit is not None and self.search_edit.isVisible():
            if self.dock is not None:
                self.dock.show()
                self.dock.raise_()
            self.search_edit.setFocus()
            self.search_edit.selectAll()

    def _say(self, text, level="info"):
        try:
            bar = self.iface.messageBar()
            {"info": bar.pushInfo, "ok": bar.pushSuccess, "warn": bar.pushWarning}[level](tr("Layers"), text)
        except Exception:  # noqa: BLE001
            pass

    def toggle_solo(self):
        if self.solo is None:
            return False
        if self.solo.active():
            self.solo.restore()
            return False
        layers = self.view.selectedLayers() or ([self.view.currentLayer()] if self.view.currentLayer() else [])
        if not layers:
            self.solo_action.setChecked(False)
            self._say(tr("Select the layer (or layers) to peek at first."))
            return False
        return self.solo.peek(layers)

    def smart_arrange(self, ask=True):
        from . import arrange

        steps = arrange.plan()
        if not steps:
            self._say(tr("There are no layers to arrange yet."))
            return False
        if arrange.same_as_plan(steps):
            self._say(tr("The layers are already arranged."), "ok")
            return False
        if ask:
            from qgis.PyQt.QtWidgets import QDialog

            dlg = arrange.preview_dialog(steps, self.iface.mainWindow())
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return False
        carried = self._badges_on_layers()
        self.arrange_snapshot = arrange.apply(steps)
        self.undo_action.setVisible(True)
        self._put_badges_back(carried)
        self._say(tr("Layers arranged by kind. “Undo arrange” in the Layers panel brings the old order back."), "ok")
        return True

    def undo_arrange(self):
        from . import arrange

        if self.arrange_snapshot is None:
            return False
        carried = self._badges_on_layers()
        arrange.undo(self.arrange_snapshot)
        self.arrange_snapshot = None
        self.undo_action.setVisible(False)
        self._put_badges_back(carried)
        return True

    def _badges_on_layers(self):
        """{layer id: [indicators]} of every layer node, except those of the engine's own indicator providers
        (they put theirs on new nodes by themselves). Arranging makes new nodes: these are moved over to them."""
        carried = {}
        for node in QgsProject.instance().layerTreeRoot().findLayers():
            keep = []
            for ind in self.view.indicators(node):
                owner = ind.parent()
                if owner is not None and owner.metaObject().className().endswith("IndicatorProvider"):
                    continue
                keep.append(ind)
            if keep:
                carried[node.layerId()] = keep
        return carried

    def _put_badges_back(self, carried):
        root = QgsProject.instance().layerTreeRoot()
        for lid, inds in carried.items():
            node = root.findLayer(lid)
            if node is None:
                continue
            have = self.view.indicators(node)
            for ind in inds:
                try:
                    if ind not in have:
                        self.view.addIndicator(node, ind)
                except RuntimeError:  # deleted meanwhile
                    pass

    # ---- unload ------------------------------------------------------------------------------------------------
    def teardown(self):
        for sig, slot in self.conns:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        theme = getattr(self.plugin, "theme", None)
        listeners = getattr(theme, "_listeners", None)
        if listeners is not None and self.refresh_theme in listeners:
            listeners.remove(self.refresh_theme)
        for name, fn in (("solo", self._teardown_solo), ("search", self._teardown_search),
                         ("empty", self._teardown_empty), ("health", lambda: self.health and self.health.teardown()),
                         ("origin", lambda: self.origin and self.origin.teardown()),
                         ("buttons", self._teardown_actions), ("icons", lambda: self.look and self.look.restore())):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                _log(f"Layers panel: {name} not removed cleanly ({e!r})")

    def _teardown_solo(self):
        if self.solo is not None and self.solo.active():
            self.solo.restore()
        if self.alt_click is not None:
            self.view.viewport().removeEventFilter(self.alt_click)

    def _teardown_search(self):
        if self.search is not None:
            self.search.teardown()
        for w in (self.search_edit, self.no_match):
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _teardown_empty(self):
        if self.empty is not None:
            self.empty.hide()
            self.empty.setParent(None)
            self.empty.deleteLater()
        self.view.setVisible(True)

    def _teardown_actions(self):
        for act in self.own_actions:
            if self.toolbar is not None:
                self.toolbar.removeAction(act)
            act.deleteLater()
        self.own_actions = []
