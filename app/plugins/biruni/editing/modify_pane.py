"""The Modify Features pane (تعديل العناصر), modelled on ArcGIS Pro's.

The main view lists the tools by section (Alignment, Construct, Divide, Reshape, Combine, Attributes,
Clean up) with favourites pinned on top and an Arabic-aware search. Choosing a tool opens its view: a
header with a back arrow and the tool's icon and name, message strips (green when a change is done, blue
when something is needed first, amber when it cannot be done), the selected features (by layer, with a
check box each, click to flash, double click to zoom), the tool's options and Apply / Reset.

Keys: Enter applies, Esc goes back, F2 finishes a sketch. The last tool and the options of each tool are
remembered for the session; favourites are kept in the settings.

Nothing is built before the pane is first opened; map tools are set only while an interactive tool is
open, and every change goes through the layer's edit buffer in one edit command (one Undo step).
"""
from qgis.core import QgsApplication, QgsIconUtils, QgsProject, QgsSettings, QgsUnitTypes, QgsVectorLayer
from qgis.PyQt.QtCore import QSize, Qt, QTimer
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox,
                                 QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                                 QListWidget, QListWidgetItem, QMenu, QPushButton, QScrollArea,
                                 QSpinBox, QStackedWidget, QTableWidget, QTableWidgetItem, QToolButton,
                                 QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import icons
from ..arabic_text import normalise
from ..i18n import tr
from . import modify_geom as mg
from . import modify_tools as mt
from .strip import INFO, SUCCESS, WARNING, StripArea

KEY = "biruni/editing/modify/"
_PANES = {}
_SESSION = {"last": None, "opts": {}}       # remembered while the program runs

ROLE_TOOL = Qt.ItemDataRole.UserRole
ROLE_LAYER = Qt.ItemDataRole.UserRole + 1
ROLE_FID = Qt.ItemDataRole.UserRole + 2


def _tokens(plugin):
    from ..theme import TOKENS

    try:
        return plugin.theme.tokens()
    except Exception:  # noqa: BLE001 - no theme: the light one
        return TOKENS["light"]


def _is_dark(plugin):
    from ..theme import TOKENS

    return _tokens(plugin).get("base") == TOKENS["dark"]["base"]


def pane_qss(t):
    from ..tracking import panel_qss

    return panel_qss(t) + f"""
#BiruniGps {{ background: {t['panel']}; }}
#BiruniGps QStackedWidget, #BiruniGps QScrollArea, #BiruniGps QWidget#ModifyPage {{ background: {t['panel']}; }}
#BiruniGps QTreeWidget#ModifyList, #BiruniGps QTreeWidget#ModifySel {{ border: none; background: {t['panel']};
    outline: none; }}
#BiruniGps QTreeWidget#ModifyList::item {{ padding: 4px 2px; border: none; }}
#BiruniGps QTreeWidget#ModifyList::item:hover, #BiruniGps QTreeWidget#ModifySel::item:hover {{
    background: {t['hover']}; }}
#BiruniGps QTreeWidget#ModifyList::item:selected, #BiruniGps QTreeWidget#ModifySel::item:selected {{
    background: {t['accent_soft']}; color: {t['text']}; }}
#BiruniGps QFrame#ModifyHeader {{ border: none; border-bottom: 1px solid {t['border']}; }}
#BiruniGps QLabel#ModifyTitle {{ font-weight: 600; font-size: 10.5pt; color: {t['text']}; }}
#BiruniGps QFrame#ModifySelRow {{ border: none; border-bottom: 1px solid {t['border']}; }}
#BiruniGps QLabel#ModifySelTitle {{ font-weight: 600; color: {t['text']}; }}
#BiruniGps QLabel#ModifySection {{ font-weight: 600; color: {t['text_dim']}; background: {t['hover']};
    padding: 0px 8px; }}
#BiruniGps QFrame#ModifyButtons {{ border: none; border-top: 1px solid {t['border']}; }}
#BiruniGps QPushButton#ModifySecondary {{ background: {t['button']}; color: {t['text']}; border: 1px solid
    {t['border']}; border-radius: 6px; padding: 6px 14px; }}
#BiruniGps QPushButton#ModifySecondary:hover {{ background: {t['hover']}; }}
#BiruniGps QPushButton#GpsPrimary:disabled {{ background: {t['mid']}; color: {t['text_disabled']}; }}
#BiruniGps QLineEdit#ModifySearch {{ border: 1px solid {t['border']}; border-radius: 6px; padding: 5px 6px;
    background: {t['base']}; }}
#BiruniGps QLineEdit#ModifySearch:focus {{ border-color: {t['accent']}; }}
#BiruniGps QToolButton#ModifyFlat {{ border: none; border-radius: 4px; background: transparent; padding: 2px; }}
#BiruniGps QToolButton#ModifyFlat:hover {{ background: {t['hover']}; }}
"""


def favourites():
    raw = QgsSettings().value(KEY + "favourites", "") or ""
    return [x for x in str(raw).split(",") if x]


def set_favourites(ids):
    QgsSettings().setValue(KEY + "favourites", ",".join(dict.fromkeys(ids)))


class ModifyFeaturesPane(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("Modify Features"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setObjectName("BiruniModifyFeatures")
        self.canvas = self.iface.mapCanvas()
        self.current = None           # the open Tool
        self.widgets = {}             # option key -> widget
        self.unchecked = set()        # (layer id, fid) left out by the user
        self.map_tool = None
        self.prev_map_tool = None
        self.pivot = None
        self.source = None
        self._band = None
        self._pivot_band = None
        self._connected = []
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(150)
        self._preview_timer.timeout.connect(self.update_preview)
        self._sel_timer = QTimer(self)
        self._sel_timer.setSingleShot(True)
        self._sel_timer.setInterval(60)
        self._sel_timer.timeout.connect(self.selection_changed)
        self._build()
        self.fill_list()
        self._connect()

    # ---- building -------------------------------------------------------------------------------------
    def _icon(self, name, colour=None):
        t = _tokens(self.plugin)
        colour = colour or t.get("icon", "#33414C")
        return icons.icon(name, colour) if icons.exists(name) else icons.icon("point", colour)

    def _build(self):
        body = QWidget()
        body.setObjectName("BiruniGps")
        outer = QVBoxLayout(body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.stack.addWidget(self._build_list_page())
        self.stack.addWidget(self._build_tool_page())
        self.setWidget(body)
        self._body = body

    def _build_list_page(self):
        page = QWidget()
        page.setObjectName("ModifyPage")
        v = QVBoxLayout(page)
        v.setContentsMargins(10, 10, 10, 6)
        v.setSpacing(8)
        self.search = QLineEdit()
        self.search.setObjectName("ModifySearch")
        self.search.setPlaceholderText(tr("Search tools"))
        self.search.setClearButtonEnabled(True)
        self._search_action = self.search.addAction(self._icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self.filter)
        v.addWidget(self.search)
        self.list = QTreeWidget()
        self.list.setObjectName("ModifyList")
        self.list.setHeaderHidden(True)
        self.list.setColumnCount(2)
        self.list.setRootIsDecorated(False)
        self.list.setIndentation(12)
        self.list.setIconSize(QSize(20, 20))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.header().setStretchLastSection(False)
        self.list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.list.header().resizeSection(1, 28)
        self.list.itemClicked.connect(self._on_list_click)
        self.list.itemActivated.connect(lambda it, _c=0: self._open_item(it))
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        v.addWidget(self.list, 1)
        return page

    def _build_tool_page(self):
        page = QWidget()
        page.setObjectName("ModifyPage")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        head = QFrame()
        head.setObjectName("ModifyHeader")
        h = QHBoxLayout(head)
        h.setContentsMargins(6, 6, 6, 6)
        self.back = QToolButton()
        self.back.setObjectName("ModifyFlat")
        self.back.setIconSize(QSize(18, 18))
        self.back.setToolTip(tr("Back to the tools (Esc)"))
        self.back.clicked.connect(self.go_back)
        h.addWidget(self.back)
        h.addStretch(1)
        self.tool_icon = QLabel()
        self.tool_icon.setFixedSize(22, 22)
        h.addWidget(self.tool_icon)
        self.tool_name = QLabel()
        self.tool_name.setObjectName("ModifyTitle")
        h.addWidget(self.tool_name)
        h.addStretch(1)
        balance = QWidget()
        balance.setFixedWidth(self.back.sizeHint().width())
        h.addWidget(balance)
        v.addWidget(head)

        inner = QWidget()
        inner.setObjectName("ModifyPage")
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(10, 8, 10, 8)
        iv.setSpacing(8)
        self.strips = StripArea()
        iv.addWidget(self.strips)

        selrow = QFrame()
        selrow.setObjectName("ModifySelRow")
        sh = QHBoxLayout(selrow)
        sh.setContentsMargins(0, 2, 0, 4)
        self.sel_icon = QLabel()
        self.sel_icon.setFixedSize(20, 20)
        sh.addWidget(self.sel_icon)
        self.sel_title = QLabel(tr("Select a feature"))
        self.sel_title.setObjectName("ModifySelTitle")
        sh.addWidget(self.sel_title)
        self.sel_count = QLabel()
        self.sel_count.setObjectName("GpsHint")
        sh.addWidget(self.sel_count)
        sh.addStretch(1)
        self.chevron = QToolButton()
        self.chevron.setObjectName("ModifyFlat")
        self.chevron.setCheckable(True)
        self.chevron.setChecked(True)
        self.chevron.setToolTip(tr("Show or hide the selected features"))
        self.chevron.toggled.connect(self._toggle_selection)
        sh.addWidget(self.chevron)
        iv.addWidget(selrow)
        self.sel_row = selrow

        self.sel = QTreeWidget()
        self.sel.setObjectName("ModifySel")
        self.sel.setHeaderHidden(True)
        self.sel.setIndentation(14)
        self.sel.setIconSize(QSize(16, 16))
        self.sel.itemClicked.connect(self._flash)
        self.sel.itemDoubleClicked.connect(self._zoom)
        self.sel.itemChanged.connect(self._sel_checked)
        iv.addWidget(self.sel)

        self.opts_box = QWidget()
        self.opts_form = QFormLayout(self.opts_box)
        self.opts_form.setContentsMargins(0, 4, 0, 0)
        self.opts_form.setHorizontalSpacing(10)
        self.opts_form.setVerticalSpacing(8)
        self.opts_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        iv.addWidget(self.opts_box)
        self.hint = QLabel()
        self.hint.setObjectName("GpsHint")
        self.hint.setWordWrap(True)
        iv.addWidget(self.hint)
        iv.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)

        buttons = QFrame()
        buttons.setObjectName("ModifyButtons")
        bh = QHBoxLayout(buttons)
        bh.setContentsMargins(10, 8, 10, 8)
        bh.addStretch(1)
        self.reset_btn = QPushButton(tr("Reset"))
        self.reset_btn.setObjectName("ModifySecondary")
        self.reset_btn.setToolTip(tr("Back to the default values"))
        self.reset_btn.clicked.connect(self.reset_options)
        bh.addWidget(self.reset_btn)
        self.apply_btn = QPushButton(tr("Apply"))
        self.apply_btn.setObjectName("GpsPrimary")
        self.apply_btn.setToolTip(tr("Apply (Enter)"))
        self.apply_btn.clicked.connect(self.apply)
        bh.addWidget(self.apply_btn)
        v.addWidget(buttons)
        self.buttons = buttons
        return page

    # ---- theme ----------------------------------------------------------------------------------------
    def refresh_theme(self):
        t = _tokens(self.plugin)
        self._body.setStyleSheet(pane_qss(t))
        self.strips.set_theme(_is_dark(self.plugin))
        self.search.removeAction(self._search_action)
        self._search_action = self.search.addAction(self._icon("search", t.get("placeholder")),
                                                    QLineEdit.ActionPosition.LeadingPosition)
        self.back.setIcon(self._icon("arrow-right" if self.isRightToLeft() else "arrow-left"))
        self.sel_icon.setPixmap(self._icon("edit/select-feature").pixmap(20, 20))
        self._chevron_icon()
        self.fill_list()
        if self.current is not None:
            self.tool_icon.setPixmap(self._icon(self.current.icon).pixmap(22, 22))
            self.fill_selection()

    def _chevron_icon(self):
        self.chevron.setIcon(self._icon("chevron-up" if self.chevron.isChecked() else "chevron-down"))

    # ---- the list of tools ----------------------------------------------------------------------------
    def fill_list(self):
        t = _tokens(self.plugin)
        favs = favourites()
        all_tools = mt.tools()
        self.list.clear()
        def header(text):
            it = QTreeWidgetItem([text, ""])
            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
            it.setSizeHint(0, QSize(0, 28))
            self.list.addTopLevelItem(it)
            it.setFirstColumnSpanned(True)
            band = QLabel(text)
            band.setObjectName("ModifySection")
            band.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeading)
            self.list.setItemWidget(it, 0, band)
            return it

        def row(parent, tool_):
            it = QTreeWidgetItem([tool_.name, ""])
            it.setIcon(0, self._icon(tool_.icon))
            it.setData(0, ROLE_TOOL, tool_.id)
            it.setToolTip(0, tool_.hint or tool_.name)
            fav = tool_.id in favs
            it.setIcon(1, self._icon("edit/favourite") if fav else self._icon("star", t.get("border")))
            it.setToolTip(1, tr("Remove from favourites") if fav else tr("Add to favourites"))
            it.setSizeHint(0, QSize(0, 30))
            parent.addChild(it)
            return it

        if favs:
            top = header(tr("Favourites"))
            for tid in favs:
                tl = mt.tool(tid)
                if tl is not None:
                    row(top, tl)
        for sid in mt.SECTION_IDS:
            top = header(mt.section_title(sid))
            for tl in all_tools:
                if tl.section == sid:
                    row(top, tl)
        self.list.expandAll()
        self.filter(self.search.text())
        last = _SESSION.get("last")
        if last:
            it = self._item_for(last)
            if it is not None:
                self.list.setCurrentItem(it)

    def _item_for(self, tid):
        for i in range(self.list.topLevelItemCount()):
            top = self.list.topLevelItem(i)
            for j in range(top.childCount()):
                if top.child(j).data(0, ROLE_TOOL) == tid and top.text(0) != tr("Favourites"):
                    return top.child(j)
        return None

    def filter(self, text):
        q = normalise(text or "")
        by_id = {tl.id: tl for tl in mt.tools()}
        for i in range(self.list.topLevelItemCount()):
            top = self.list.topLevelItem(i)
            shown = 0
            for j in range(top.childCount()):
                it = top.child(j)
                tl = by_id.get(it.data(0, ROLE_TOOL))
                hay = normalise(" ".join([tl.name, tl.words, tl.id.replace("_", " "), mt.section_title(tl.section)]))
                ok = not q or all(w in hay for w in q.split())
                it.setHidden(not ok)
                shown += ok
            top.setHidden(shown == 0)

    def visible_tools(self):
        out = []
        for i in range(self.list.topLevelItemCount()):
            top = self.list.topLevelItem(i)
            if top.isHidden():
                continue
            out += [top.child(j).data(0, ROLE_TOOL) for j in range(top.childCount()) if not top.child(j).isHidden()]
        return out

    def section_names(self):
        return [self.list.topLevelItem(i).text(0) for i in range(self.list.topLevelItemCount())
                if not self.list.topLevelItem(i).isHidden()]

    def _on_list_click(self, item, column):
        tid = item.data(0, ROLE_TOOL)
        if not tid:
            return
        if column == 1:
            self.toggle_favourite(tid)
            return
        self.open_tool(tid)

    def _open_item(self, item):
        tid = item.data(0, ROLE_TOOL) if item is not None else None
        if tid:
            self.open_tool(tid)

    def _list_menu(self, pos):
        it = self.list.itemAt(pos)
        tid = it.data(0, ROLE_TOOL) if it is not None else None
        if not tid:
            return
        m = QMenu(self)
        fav = tid in favourites()
        a = m.addAction(tr("Remove from favourites") if fav else tr("Add to favourites"))
        a.triggered.connect(lambda: self.toggle_favourite(tid))
        m.exec(self.list.viewport().mapToGlobal(pos))

    def toggle_favourite(self, tid):
        favs = favourites()
        if tid in favs:
            favs.remove(tid)
        else:
            favs.append(tid)
        set_favourites(favs)
        QTimer.singleShot(0, self.fill_list)   # not inside the click on the item being replaced

    # ---- opening a tool -------------------------------------------------------------------------------
    def open_tool(self, tid):
        tl = mt.tool(tid)
        if tl is None:
            return None
        if self.current is not None:
            self._leave_tool()
        self.current = tl
        _SESSION["last"] = tid
        self.pivot = None
        self.source = None
        self.tool_icon.setPixmap(self._icon(tl.icon).pixmap(22, 22))
        self.tool_name.setText(tl.name)
        self.strips.clear()
        self.hint.setText(tl.hint)
        self.hint.setVisible(bool(tl.hint))
        self.buttons.setVisible(tl.applies())
        self.stack.setCurrentIndex(1)
        self.fill_selection()
        self._build_options()
        self._install_map_tool()
        self._need_selection_strip()
        self._schedule_preview()
        return tl

    def go_back(self):
        if self.current is not None:
            self._leave_tool()
        self.current = None
        self.stack.setCurrentIndex(0)
        it = self._item_for(_SESSION.get("last")) if _SESSION.get("last") else None
        if it is not None:
            self.list.setCurrentItem(it)
        self.search.setFocus()

    def _leave_tool(self):
        self._remember_options()
        self._clear_preview()
        self._remove_map_tool()
        self.strips.clear()

    def in_tool(self):
        return self.stack.currentIndex() == 1 and self.current is not None

    # ---- the selection --------------------------------------------------------------------------------
    def selected_layers(self):
        root = QgsProject.instance().layerTreeRoot()
        order = root.layerOrder() if root is not None else list(QgsProject.instance().mapLayers().values())
        return [lyr for lyr in order if isinstance(lyr, QgsVectorLayer) and lyr.isSpatial() and
                lyr.selectedFeatureCount() > 0]

    def selection(self):
        """{layer: [checked selected feature ids]}."""
        out = {}
        for lyr in self.selected_layers():
            fids = [fid for fid in sorted(lyr.selectedFeatureIds()) if (lyr.id(), fid) not in self.unchecked]
            if fids:
                out[lyr] = fids
        return out

    def fill_selection(self):
        self.sel.blockSignals(True)
        self.sel.clear()
        total = 0
        for lyr in self.selected_layers():
            fids = sorted(lyr.selectedFeatureIds())
            total += len(fids)
            top = QTreeWidgetItem([f"{lyr.name()}  ({len(fids)})"])
            top.setIcon(0, QgsIconUtils.iconForLayer(lyr))
            top.setData(0, ROLE_LAYER, lyr.id())
            f = QFont(self.sel.font())
            f.setBold(True)
            top.setFont(0, f)
            self.sel.addTopLevelItem(top)
            from qgis.core import QgsFeatureRequest

            shown = fids[:300]
            for feat in sorted(lyr.getFeatures(QgsFeatureRequest().setFilterFids(shown)), key=lambda x: x.id()):
                it = QTreeWidgetItem([mt.display_name(lyr, feat)])
                it.setData(0, ROLE_LAYER, lyr.id())
                it.setData(0, ROLE_FID, feat.id())
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(0, Qt.CheckState.Unchecked if (lyr.id(), feat.id()) in self.unchecked
                                 else Qt.CheckState.Checked)
                top.addChild(it)
            if len(fids) > len(shown):
                more = QTreeWidgetItem([tr("… and {n} more").format(n=len(fids) - len(shown))])
                more.setFlags(Qt.ItemFlag.ItemIsEnabled)
                top.addChild(more)
            top.setExpanded(True)
        self.sel.blockSignals(False)
        rows = sum(1 + self.sel.topLevelItem(i).childCount() for i in range(self.sel.topLevelItemCount()))
        row_h = self.sel.sizeHintForRow(0) if self.sel.topLevelItemCount() else 20
        self.sel.setFixedHeight(min(170, max(28, rows * max(row_h, 18) + 6)))
        n = sum(len(v) for v in self.selection().values())
        self.sel_count.setText(tr("{n} of {t}").format(n=n, t=total) if total else "")
        self.sel.setVisible(self.chevron.isChecked() and total > 0)
        self._refresh_feature_options()

    def _toggle_selection(self, on):
        self._chevron_icon()
        self.sel.setVisible(on and self.sel.topLevelItemCount() > 0)

    def _sel_checked(self, item, _col=0):
        fid = item.data(0, ROLE_FID)
        if fid is None:
            return
        key = (item.data(0, ROLE_LAYER), fid)
        if item.checkState(0) == Qt.CheckState.Checked:
            self.unchecked.discard(key)
        else:
            self.unchecked.add(key)
        self.sel_count.setText(tr("{n} of {t}").format(n=sum(len(v) for v in self.selection().values()),
                                                         t=sum(lyr.selectedFeatureCount() for lyr in self.selected_layers())))
        self._refresh_feature_options()
        self._schedule_preview()

    def _flash(self, item, _col=0):
        fid = item.data(0, ROLE_FID)
        lyr = QgsProject.instance().mapLayer(item.data(0, ROLE_LAYER) or "")
        if fid is not None and lyr is not None:
            self.canvas.flashFeatureIds(lyr, [fid])

    def _zoom(self, item, _col=0):
        fid = item.data(0, ROLE_FID)
        lyr = QgsProject.instance().mapLayer(item.data(0, ROLE_LAYER) or "")
        if lyr is None:
            return
        self.canvas.zoomToFeatureIds(lyr, [fid] if fid is not None else list(lyr.selectedFeatureIds()))

    def selection_changed(self, *args):
        if not self.in_tool():
            return
        live = {(lyr.id(), fid) for lyr in self.selected_layers() for fid in lyr.selectedFeatureIds()}
        self.unchecked &= live
        self.fill_selection()
        self._need_selection_strip()
        if self.map_tool is None:
            self._install_map_tool()
        self._schedule_preview()

    def _need_selection_strip(self):
        if self.current is None:
            return
        if self.current.selection and not self.selection():
            self.strips.show_message(INFO, tr("Make a selection."))
        else:
            self.strips.clear(INFO)

    # ---- options --------------------------------------------------------------------------------------
    def _unit(self):
        crs = self.canvas.mapSettings().destinationCrs()
        u = crs.mapUnits() if crs.isValid() else None
        return QgsUnitTypes.toAbbreviatedString(u) if u is not None else ""

    def _saved(self, tid):
        vals = dict(mt.defaults(tid))
        vals.update(_SESSION["opts"].get(tid, {}))
        return vals

    def _build_options(self):
        while self.opts_form.rowCount():
            self.opts_form.removeRow(0)
        self.widgets = {}
        tl = self.current
        saved = self._saved(tl.id)
        unit = self._unit()
        for o in mt.options(tl.id):
            w = self._option_widget(o, saved.get(o.key, o.default), unit)
            if w is None:
                continue
            self.widgets[o.key] = w
            if o.tip:
                w.setToolTip(o.tip)
            if o.kind in ("bool", "rules", "vertices") or not o.label:
                self.opts_form.addRow(w)
            else:
                self.opts_form.addRow(o.label, w)
        self.opts_box.setVisible(self.opts_form.rowCount() > 0)
        self._option_visibility()

    def _option_visibility(self):
        """Show only the options that matter for the current choices (e.g. Parts or Distance)."""
        along = self.widgets.get("along")
        if not isinstance(along, QComboBox):
            return
        parts = along.currentData() == "parts"
        for key, show in (("parts", parts), ("spacing", not parts)):
            w = self.widgets.get(key)
            if w is not None:
                w.setVisible(show)
                lab = self.opts_form.labelForField(w)
                if lab is not None:
                    lab.setVisible(show)

    def _option_widget(self, o, value, unit):
        changed = self._option_changed
        if o.kind in ("length", "angle", "number"):
            w = QDoubleSpinBox()
            w.setRange(o.lo if o.lo is not None else -1e12, o.hi if o.hi is not None else 1e12)
            w.setDecimals(o.decimals)
            w.setValue(float(value if value is not None else 0))
            if o.kind == "length" and unit:
                w.setSuffix(" " + unit)
            elif o.kind == "angle":
                w.setSuffix(" °")
            w.setKeyboardTracking(False)
            w.valueChanged.connect(changed)
            return w
        if o.kind == "int":
            w = QSpinBox()
            w.setRange(int(o.lo or 0), int(o.hi or 1000000))
            w.setValue(int(value or 0))
            w.setKeyboardTracking(False)
            w.valueChanged.connect(changed)
            return w
        if o.kind == "choice":
            w = QComboBox()
            for val, label in o.choices:
                w.addItem(label, val)
            w.setCurrentIndex(max(0, w.findData(value)))
            w.currentIndexChanged.connect(changed)
            return w
        if o.kind == "bool":
            w = QCheckBox(o.label)
            w.setChecked(bool(value))
            w.toggled.connect(changed)
            return w
        if o.kind == "text":
            w = QLineEdit("" if value is None else str(value))
            w.textChanged.connect(changed)
            return w
        if o.kind == "point_layer":
            w = QComboBox()
            for lyr in QgsProject.instance().mapLayers().values():
                if mt.layer_kind(lyr) == "point":
                    w.addItem(QgsIconUtils.iconForLayer(lyr), lyr.name(), lyr.id())
            idx = w.findData(value) if value else -1
            if idx < 0:
                for i in range(w.count()):
                    lyr = QgsProject.instance().mapLayer(w.itemData(i))
                    if lyr is not None and lyr.isEditable():
                        idx = i
                        break
            w.setCurrentIndex(max(0, idx))
            w.currentIndexChanged.connect(changed)
            return w
        if o.kind in ("field", "feature"):
            w = QComboBox()
            w.setProperty("saved", value)
            w.currentIndexChanged.connect(changed)
            return w
        if o.kind == "rules":
            w = QTableWidget(0, 2)
            w.setHorizontalHeaderLabels([tr("Field"), tr("Value of the merged feature")])
            w.horizontalHeader().setStretchLastSection(True)
            w.verticalHeader().setVisible(False)
            w.setMinimumHeight(140)
            w.setProperty("saved", value or {})
            return w
        if o.kind == "fields":
            w = QListWidget()
            w.setMinimumHeight(110)
            w.setProperty("saved", value)
            w.itemChanged.connect(changed)
            return w
        if o.kind == "vertices":
            box = QWidget()
            bv = QVBoxLayout(box)
            bv.setContentsMargins(0, 0, 0, 0)
            table = QTableWidget(0, 2)
            table.setObjectName("ModifyVertices")
            table.setHorizontalHeaderLabels(["X", "Y"])
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.setMinimumHeight(180)
            bv.addWidget(table)
            btn = QPushButton(tr("Drag vertices on the map (vertex tool)"))
            btn.setObjectName("ModifySecondary")
            btn.clicked.connect(self._vertex_tool)
            bv.addWidget(btn)
            box.table = table
            return box
        return None

    def _vertex_tool(self):
        act = getattr(self.iface, "actionVertexTool", None)
        if act is not None:
            act().trigger()

    def _refresh_feature_options(self):
        """Options that depend on the selection: fields, features to keep, rules, vertex table."""
        if self.current is None:
            return
        sel = self.selection()
        first = next(iter(sel), None) or (self.iface.activeLayer() if hasattr(self.iface, "activeLayer") else None)
        for key, w in self.widgets.items():
            o = next((x for x in mt.options(self.current.id) if x.key == key), None)
            if o is None:
                continue
            w.blockSignals(True)
            if o.kind == "field":
                keep = w.currentText() or w.property("saved")
                w.clear()
                if isinstance(first, QgsVectorLayer):
                    for f in first.fields():
                        w.addItem(f.displayName(), f.name())
                w.setCurrentIndex(max(0, w.findData(keep)))
            elif o.kind == "feature":
                keep = w.currentData() or w.property("saved")
                w.clear()
                from qgis.core import QgsFeatureRequest

                for lyr, fids in sel.items():
                    for feat in lyr.getFeatures(QgsFeatureRequest().setFilterFids(fids[:300])):
                        w.addItem(f"{lyr.name()}: {mt.display_name(lyr, feat)}", f"{lyr.id()}|{feat.id()}")
                idx = w.findData(keep) if keep else -1
                w.setCurrentIndex(max(0, idx))
            elif o.kind == "rules":
                saved = self._rules_value(w) or w.property("saved") or {}
                w.setRowCount(0)
                if isinstance(first, QgsVectorLayer):
                    pk = set(first.primaryKeyAttributes())
                    for i, f in enumerate(first.fields()):
                        if i in pk:
                            continue
                        r = w.rowCount()
                        w.insertRow(r)
                        it = QTableWidgetItem(f.displayName())
                        it.setData(Qt.ItemDataRole.UserRole, f.name())
                        it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                        w.setItem(r, 0, it)
                        cb = QComboBox()
                        for rid, label in mt.rule_items():
                            cb.addItem(label, rid)
                        cb.setCurrentIndex(max(0, cb.findData(saved.get(f.name(), "main"))))
                        cb.currentIndexChanged.connect(self._option_changed)
                        w.setCellWidget(r, 1, cb)
            elif o.kind == "fields":
                lyr = self.iface.activeLayer() if hasattr(self.iface, "activeLayer") else None
                saved = w.property("saved")
                w.clear()
                if isinstance(lyr, QgsVectorLayer):
                    pk = set(lyr.primaryKeyAttributes())
                    for i, f in enumerate(lyr.fields()):
                        if i in pk:
                            continue
                        it = QListWidgetItem(f.displayName())
                        it.setData(Qt.ItemDataRole.UserRole, f.name())
                        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        on = not saved or f.name() in saved
                        it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                        w.addItem(it)
            elif o.kind == "vertices":
                self._fill_vertices(w.table, sel)
            w.blockSignals(False)

    def _fill_vertices(self, table, sel):
        table.setRowCount(0)
        table.setProperty("rows", [])
        feats = [(lyr, fid) for lyr, fids in sel.items() for fid in fids]
        if len(feats) != 1:
            return
        lyr, fid = feats[0]
        g = lyr.getFeature(fid).geometry()
        rows = mg.vertex_rows(g)
        dec = 8 if lyr.crs().isGeographic() else 3
        for i, x, y in rows:
            r = table.rowCount()
            table.insertRow(r)
            table.setVerticalHeaderItem(r, QTableWidgetItem(str(r + 1)))
            for c, val in enumerate((x, y)):
                text = f"{val:.{dec}f}"
                it = QTableWidgetItem(text)
                it.setData(Qt.ItemDataRole.UserRole, val)
                it.setData(Qt.ItemDataRole.UserRole + 1, text)  # as shown: an untouched cell is not a move
                table.setItem(r, c, it)
        table.setProperty("rows", [i for i, _x, _y in rows])

    def _rules_value(self, w):
        out = {}
        for r in range(w.rowCount()):
            it = w.item(r, 0)
            cb = w.cellWidget(r, 1)
            if it is not None and cb is not None:
                out[it.data(Qt.ItemDataRole.UserRole)] = cb.currentData()
        return out

    def values(self):
        """The current option values of the open tool."""
        if self.current is None:
            return {}
        out = {}
        for o in mt.options(self.current.id):
            w = self.widgets.get(o.key)
            if w is None:
                continue
            if isinstance(w, (QDoubleSpinBox, QSpinBox)):
                out[o.key] = w.value()
            elif isinstance(w, QCheckBox):
                out[o.key] = w.isChecked()
            elif isinstance(w, QLineEdit):
                out[o.key] = w.text()
            elif isinstance(w, QComboBox):
                out[o.key] = w.currentData()
            elif isinstance(w, QTableWidget):
                out[o.key] = self._rules_value(w)
            elif isinstance(w, QListWidget):
                out[o.key] = [w.item(i).data(Qt.ItemDataRole.UserRole) for i in range(w.count())
                              if w.item(i).checkState() == Qt.CheckState.Checked]
            elif o.kind == "vertices":
                out["moves"] = self._vertex_moves(w.table)
        return out

    def _vertex_moves(self, table):
        from .entry import parse_coordinate

        moves = {}
        idx = table.property("rows") or []
        for r in range(table.rowCount()):
            cells = [table.item(r, c) for c in (0, 1)]
            if all(it is not None and it.text().strip() == it.data(Qt.ItemDataRole.UserRole + 1) for it in cells):
                continue  # not edited: the vertex keeps its full precision (the table shows it rounded)
            xy = []
            for c, it in enumerate(cells):
                if it is not None and it.text().strip() == it.data(Qt.ItemDataRole.UserRole + 1):
                    xy.append(it.data(Qt.ItemDataRole.UserRole))  # the other coordinate of an edited row
                    continue
                try:
                    xy.append(parse_coordinate(it.text()))
                except (ValueError, AttributeError):
                    raise mt.Refusal(tr("Cannot read the coordinate in row {n}.").format(n=r + 1), WARNING) from None
            moves[idx[r]] = tuple(xy)
        return moves

    def set_value(self, key, value):
        """Set an option from code (tests, scripts)."""
        w = self.widgets.get(key)
        if isinstance(w, (QDoubleSpinBox, QSpinBox)):
            w.setValue(value)
        elif isinstance(w, QCheckBox):
            w.setChecked(bool(value))
        elif isinstance(w, QLineEdit):
            w.setText(str(value))
        elif isinstance(w, QComboBox):
            w.setCurrentIndex(max(0, w.findData(value)))

    def _option_changed(self, *args):
        self._remember_options()
        self._option_visibility()
        if self.current is not None and self.current.id in ("transform", "repeat_copy") and \
                self.values().get("pivot") != "click":
            self._show_pivot(None)
        self._schedule_preview()

    def _remember_options(self):
        if self.current is None:
            return
        vals = {k: v for k, v in self.values().items() if k != "moves"}
        _SESSION["opts"][self.current.id] = vals

    def reset_options(self):
        if self.current is None:
            return
        _SESSION["opts"].pop(self.current.id, None)
        self.pivot = None
        self._show_pivot(None)
        self._build_options()
        self._refresh_feature_options()
        self.strips.clear(SUCCESS)
        self._schedule_preview()

    # ---- running --------------------------------------------------------------------------------------
    def context(self):
        active = self.iface.activeLayer() if hasattr(self.iface, "activeLayer") else None
        ctx = mt.Context(self.canvas, self.selection(), active if isinstance(active, QgsVectorLayer) else None,
                         self.pivot)
        ctx.source = self.source
        return ctx

    def apply(self, inputs=None):
        """Apply the open tool; returns the number of features changed (0 when refused)."""
        if self.current is None:
            return 0
        tl = self.current
        ctx = self.context()
        try:
            out = mt.run(tl.id, ctx, self.values(), inputs)
            if out.extra == "source":
                self.source = ctx.source
                lyr, f = ctx.source
                self.strips.clear(WARNING)
                self.strips.show_message(INFO, tr("Source: {f} of “{l}”. Now click the features to fill; Esc "
                                                  "to pick another source.").format(f=mt.display_name(lyr, f),
                                                                                     l=lyr.name()))
                return 0
            n = mt.write(out, tl.name, ctx, self.values().get("chord", 0.01))
        except mt.Refusal as ex:
            self.strips.clear(SUCCESS)
            self.strips.show_message(INFO if ex.level == "info" else WARNING, ex.message)
            return 0
        self.strips.clear(WARNING)
        if tl.id != "transfer":
            self.strips.clear(INFO)
        msg = tr("{tool} completed: 1 feature.") if n == 1 else tr("{tool} completed: {n} features.")
        if out.skipped:
            msg += " " + tr("{n} left as they were.").format(n=out.skipped)
        self.strips.show_message(SUCCESS, msg.format(tool=tl.name, n=n))
        self._clear_preview()
        self.canvas.refresh()
        QTimer.singleShot(0, self.selection_changed)
        if tl.id == "edit_vertices":
            self._refresh_feature_options()
        return n

    # ---- map tools ------------------------------------------------------------------------------------
    def _sketch_layer(self):
        active = self.iface.activeLayer() if hasattr(self.iface, "activeLayer") else None
        sel = list(self.selection())
        for lyr in sel + [active]:
            if isinstance(lyr, QgsVectorLayer) and lyr.isSpatial():
                return lyr
        for lyr in self.canvas.layers():
            if isinstance(lyr, QgsVectorLayer) and lyr.isSpatial():
                return lyr
        return None

    def _install_map_tool(self):
        tl = self.current
        if tl is None or tl.interactive is None or self.map_tool is not None:
            return
        need = tl.interactive
        if need in ("point", "points2", "pivot"):
            tool = mt.ClickTool(self.canvas, 2 if need == "points2" else 1)
            tool.pointsReady.connect(self._points_ready)
        else:
            lyr = self._sketch_layer()
            if lyr is None:
                return
            cad = self.iface.cadDockWidget() if hasattr(self.iface, "cadDockWidget") else None
            shape = "line" if need == "line" else mt.layer_kind(lyr)
            tool = mt.SketchTool(self.canvas, cad, lyr, shape)
            tool.drawn.connect(self._drawn)
        tool.escape.connect(self._map_escape)
        current = self.canvas.mapTool()
        self.prev_map_tool = current if current is not None and not isinstance(current, (mt.ClickTool, mt.SketchTool)) \
            else self.prev_map_tool
        self.map_tool = tool
        self.canvas.setMapTool(tool)

    def _remove_map_tool(self):
        tool, self.map_tool = self.map_tool, None
        self._show_pivot(None)
        if tool is None:
            return
        if self.canvas.mapTool() is tool:
            self.canvas.unsetMapTool(tool)
            if self.prev_map_tool is not None:
                try:
                    self.canvas.setMapTool(self.prev_map_tool)
                except RuntimeError:     # the previous tool is gone
                    self.prev_map_tool = None
        tool.deleteLater()

    def _points_ready(self, pts):
        if self.current is None:
            return
        if self.current.interactive == "pivot":
            self.pivot = pts[0]
            self._show_pivot(pts[0])
            w = self.widgets.get("pivot")
            if isinstance(w, QComboBox):
                w.blockSignals(True)
                w.setCurrentIndex(max(0, w.findData("click")))
                w.blockSignals(False)
                self._remember_options()
            self._schedule_preview()
            return
        self.apply({"points": pts})

    def _drawn(self, geom):
        if self.current is not None:
            key = "geometry" if self.current.interactive == "draw" else "line"
            self.apply({key: geom})

    def _map_escape(self):
        if self.current is not None and self.current.id == "transfer" and self.source is not None:
            self.source = None
            self.strips.clear(INFO)
            self.strips.show_message(INFO, tr("Click the source feature."))
            return
        self.go_back()

    def finish_sketch(self):
        if isinstance(self.map_tool, mt.SketchTool):
            self.map_tool.finish()

    def _show_pivot(self, p):
        from qgis.core import Qgis, QgsPointXY
        from qgis.gui import QgsRubberBand

        if p is None:
            if self._pivot_band is not None:
                self._pivot_band.reset(Qgis.GeometryType.Point)
            return
        if self._pivot_band is None:
            self._pivot_band = QgsRubberBand(self.canvas, Qgis.GeometryType.Point)
            self._pivot_band.setIcon(QgsRubberBand.IconType.ICON_CROSS)
            self._pivot_band.setIconSize(14)
            self._pivot_band.setWidth(2)
            self._pivot_band.setColor(mt._band_colour())
        self._pivot_band.reset(Qgis.GeometryType.Point)
        self._pivot_band.addPoint(QgsPointXY(*p))

    # ---- preview --------------------------------------------------------------------------------------
    def _schedule_preview(self):
        if self.current is not None and self.current.preview:
            self._preview_timer.start()
        else:
            self._clear_preview()

    def update_preview(self):
        """Draw what Apply would do (orange) without changing anything; returns the geometries shown."""
        if self.current is None or not self.current.preview:
            self._clear_preview()
            return []
        if sum(len(v) for v in self.selection().values()) > 500:
            self._clear_preview()
            return []
        try:
            out = mt.run(self.current.id, self.context(), self.values())
            geoms = out.geometries()
        except (mt.Refusal, Exception):  # noqa: BLE001 - a preview never raises
            geoms = []
        if self._band is None:
            self._band = mt.preview_band(self.canvas)
        mt.show_preview(self._band, geoms, self.canvas.mapUnitsPerPixel())
        self.preview_geoms = geoms
        return geoms

    def _clear_preview(self):
        self.preview_geoms = []
        if self._band is not None:
            from qgis.core import Qgis

            self._band.reset(Qgis.GeometryType.Line)

    # ---- keys -----------------------------------------------------------------------------------------
    def keyPressEvent(self, e):  # noqa: N802
        if self.handle_key(e.key()):
            e.accept()
        else:
            super().keyPressEvent(e)

    def handle_key(self, key):
        if key == Qt.Key.Key_Escape:
            if self.in_tool():
                self.go_back()
                return True
            if self.search.text():
                self.search.clear()
                return True
            return False
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.in_tool():
                if self.current.applies():
                    self.apply()
                elif isinstance(self.map_tool, mt.SketchTool):
                    self.map_tool.finish()
                return True
            it = self.list.currentItem()
            tid = it.data(0, ROLE_TOOL) if it is not None and not it.isHidden() else None
            vis = self.visible_tools()
            if (tid is None or tid not in vis) and vis:
                tid = vis[0]
            if tid:
                self.open_tool(tid)
            return True
        if key == Qt.Key.Key_F2 and self.in_tool():
            self.finish_sketch()
            return True
        return False

    # ---- signals --------------------------------------------------------------------------------------
    def _connect(self):
        pairs = [(self.canvas.selectionChanged, self._sel_timer.start)]
        if hasattr(self.iface, "currentLayerChanged"):
            pairs.append((self.iface.currentLayerChanged, self._sel_timer.start))
        p = QgsProject.instance()
        pairs += [(p.layersRemoved, self._sel_timer.start), (p.cleared, self._project_cleared)]
        for sig, slot in pairs:
            sig.connect(slot)
            self._connected.append((sig, slot))

    def _project_cleared(self):
        self.unchecked.clear()
        self.source = None
        self.go_back()

    def shutdown(self):
        for sig, slot in self._connected:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connected = []
        self._preview_timer.stop()
        self._sel_timer.stop()
        if self.current is not None:
            self._leave_tool()
        for band in (self._band, self._pivot_band):
            if band is not None:
                try:
                    self.canvas.scene().removeItem(band)
                except RuntimeError:
                    pass
        self._band = self._pivot_band = None


def open_pane(plugin, tool_id=None):
    d = _PANES.get(id(plugin))
    if d is None:
        d = ModifyFeaturesPane(plugin)
        mw = plugin.iface.mainWindow()
        from ..commands import tab_beside

        tab_beside(mw, d, getattr(plugin, "toolbox", None))
        d.refresh_theme()
        _PANES[id(plugin)] = d
        QgsApplication.instance().aboutToQuit.connect(d.shutdown)
        theme = getattr(plugin, "theme", None)
        if theme is not None and hasattr(theme, "on_change"):
            theme.on_change(lambda: d.refresh_theme() if id(plugin) in _PANES else None)
    if d.isFloating():
        from ..commands import tab_beside

        tab_beside(plugin.iface.mainWindow(), d, getattr(plugin, "toolbox", None))
    d.show()
    d.raise_()
    if tool_id:
        d.open_tool(tool_id)
    return d


def close_pane(plugin):
    d = _PANES.pop(id(plugin), None)
    if d is not None:
        d.shutdown()
        plugin.iface.mainWindow().removeDockWidget(d)
        d.deleteLater()
