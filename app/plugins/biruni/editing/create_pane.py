"""The Create Features pane (إنشاء العناصر): pick a feature template, pick a construction tool, draw.

Templates are listed by layer (only layers whose features can be added), searchable in any language
(Arabic-aware). The pane is built the first time it is opened; nothing is connected or installed
before that.
"""
from qgis.core import QgsApplication, QgsIconUtils, QgsProject, QgsSettings
from qgis.PyQt.QtCore import QMetaType, QSize, Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog,
                                 QDialogButtonBox, QDockWidget, QDoubleSpinBox, QFormLayout, QFrame,
                                 QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QScrollArea,
                                 QSpinBox, QToolButton, QTreeWidget, QTreeWidgetItem,
                                 QVBoxLayout, QWidget)

from .. import icons
from ..i18n import tr
from . import construct
from . import templates as tpl

KEY = "biruni/editing/create/"
_PANES = {}

ROLE_LAYER = Qt.ItemDataRole.UserRole
ROLE_TEMPLATE = Qt.ItemDataRole.UserRole + 1


def _icon(name, plugin=None):
    color = "#475569"
    try:
        color = plugin.theme.tokens().get("icon", color)
    except Exception:  # noqa: BLE001 - a missing theme only changes the colour
        pass
    return icons.icon(name, color) if icons.exists(name) else icons.icon("point", color)


class TemplateDialog(QDialog):
    """Edit a template: name, description, tags, tool, form behaviour and the default values."""

    def __init__(self, layer, template, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Feature template"))
        self.layer = layer
        self.template = template.copy()
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(template.name)
        form.addRow(tr("Name"), self.name)
        self.description = QLineEdit(template.description)
        form.addRow(tr("Description"), self.description)
        self.tags = QLineEdit(", ".join(template.tags))
        self.tags.setPlaceholderText(tr("Words to find it by, separated by commas"))
        form.addRow(tr("Tags"), self.tags)
        self.tool = QComboBox()
        for tid, label, _icon_name in construct.tools_for(construct.geometry_kind(layer)):
            self.tool.addItem(label, tid)
        self.tool.setCurrentIndex(max(0, self.tool.findData(template.tool)))
        form.addRow(tr("Construction tool"), self.tool)
        self.form = QComboBox()
        self.form.addItem(tr("Only when a required field is empty"), tpl.FORM_AUTO)
        self.form.addItem(tr("Always"), tpl.FORM_ALWAYS)
        self.form.addItem(tr("Never"), tpl.FORM_NEVER)
        self.form.setCurrentIndex(max(0, self.form.findData(template.form)))
        form.addRow(tr("Open the attribute form"), self.form)
        v.addLayout(form)
        v.addWidget(QLabel(tr("Values of new features")))
        inner = QWidget()
        self.values_form = QFormLayout(inner)
        self.editors = {}
        for i, f in enumerate(layer.fields()):
            if i in layer.primaryKeyAttributes():
                continue
            self.editors[f.name()] = self._editor(i, f, template.attributes.get(f.name()))
            self.values_form.addRow(f.displayName(), self.editors[f.name()])
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.resize(420, 520)

    def _editor(self, i, field, value):
        setup = self.layer.editorWidgetSetup(i)
        if setup.type() == "ValueMap":
            combo = QComboBox()
            combo.addItem(tr("(empty)"), None)
            vm = setup.config().get("map") or {}
            pairs = []
            if isinstance(vm, dict):
                pairs = list(vm.items())
            else:
                for d in vm:
                    pairs.extend(d.items())
            for desc, val in pairs:
                if str(val).startswith("{2839923C"):   # QGIS's "NULL" entry
                    continue
                combo.addItem(str(desc), val)
            if value not in (None, ""):
                idx = combo.findData(value)
                if idx < 0:
                    combo.addItem(str(value), value)
                    idx = combo.count() - 1
                combo.setCurrentIndex(idx)
            return combo
        if setup.type() == "CheckBox" or field.type() == QMetaType.Type.Bool:
            box = QComboBox()
            box.addItem(tr("(empty)"), None)
            box.addItem(tr("Yes"), True)
            box.addItem(tr("No"), False)
            box.setCurrentIndex(max(0, box.findData(value)) if value is not None else 0)
            return box
        edit = QLineEdit("" if value is None else str(value))
        return edit

    def result_template(self):
        t = self.template
        t.name = self.name.text().strip() or self.layer.name()
        t.description = self.description.text().strip()
        t.tags = [w.strip() for w in self.tags.text().replace("،", ",").split(",") if w.strip()]
        t.tool = self.tool.currentData() or ""
        t.form = self.form.currentData() or tpl.FORM_AUTO
        attrs = {}
        for name, w in self.editors.items():
            val = w.currentData() if isinstance(w, QComboBox) else (w.text().strip() or None)
            if val is not None and val != "":
                attrs[name] = val
        t.attributes = attrs
        return t


class CreateFeaturesPane(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("Create Features"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setObjectName("BiruniCreateFeatures")
        self.tool = None
        self.tool_buttons = {}
        self.options = self._load_options()
        self._connected = []
        self._build()
        self.refresh()
        self._connect()

    # -- building --
    def _build(self):
        body = QWidget()
        body.setObjectName("BiruniGps")
        v = QVBoxLayout(body)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(8)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search templates"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        v.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIconSize(QSize(18, 18))
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._on_select)
        self.tree.itemDoubleClicked.connect(self._on_double)
        v.addWidget(self.tree, 2)

        row = QHBoxLayout()
        for icon, tip, fn in (("square-plus", tr("New template"), self.new_template),
                              ("palette", tr("Make templates from the layer's symbology"), self.from_symbology),
                              ("pencil", tr("Edit the template"), self.edit_template),
                              ("trash", tr("Delete the template"), self.delete_template)):
            b = QToolButton()
            b.setObjectName("Flat")
            b.setIcon(_icon(icon, self.plugin))
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)

        tools_card = QFrame()
        tools_card.setObjectName("GpsCard")
        tv = QVBoxLayout(tools_card)
        self.tools_title = QLabel(tr("Choose a template"))
        self.tools_title.setObjectName("GpsHint")
        tv.addWidget(self.tools_title)
        self.tools_grid = QGridLayout()
        self.tools_grid.setSpacing(2)
        tv.addLayout(self.tools_grid)
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        v.addWidget(tools_card)

        opts = QFrame()
        opts.setObjectName("GpsCard")
        self.opts_form = QFormLayout(opts)
        self.segment = QComboBox()
        for sid, label, icon in construct.segment_kinds():
            self.segment.addItem(_icon(icon, self.plugin), label, sid)
        self.segment.currentIndexChanged.connect(lambda: self._set("segment", self.segment.currentData()))
        self.radius = self._spin(0, 1e7, 3, "radius", tr("0 = through the clicked point"))
        self.spacing = self._spin(0.001, 1e7, 3, "spacing")
        self.parts = QSpinBox()
        self.parts.setRange(1, 10000)
        self.parts.setValue(int(self.options["parts"]))
        self.parts.valueChanged.connect(lambda x: self._set("parts", x))
        self.along = QComboBox()
        self.along.addItem(tr("Every distance"), "distance")
        self.along.addItem(tr("Equal parts"), "parts")
        self.along.setCurrentIndex(max(0, self.along.findData(self.options["along"])))
        self.along.currentIndexChanged.connect(lambda: self._set("along", self.along.currentData()))
        self.ends = QCheckBox(tr("Include the line's ends"))
        self.ends.setChecked(bool(self.options["include_ends"]))
        self.ends.toggled.connect(lambda x: self._set("include_ends", x))
        self.sides = QSpinBox()
        self.sides.setRange(3, 360)
        self.sides.setValue(int(self.options["sides"]))
        self.sides.valueChanged.connect(lambda x: self._set("sides", x))
        self.gap = self._spin(0, 1000, 3, "fill_tolerance", tr("Gaps up to this size are closed (map units)"))
        self.chord = self._spin(0.0001, 100, 4, "chord", tr("Largest gap between an arc and its straight "
                                                           "segments when the layer cannot store curves"))
        self.convention = QComboBox()
        self.convention.addItem(tr("Bearing (clockwise from north)"), "azimuth")
        self.convention.addItem(tr("Angle (counter-clockwise from east)"), "polar")
        self.convention.setCurrentIndex(max(0, self.convention.findData(self.options["convention"])))
        self.convention.currentIndexChanged.connect(lambda: self._set("convention", self.convention.currentData()))
        self.dms = QCheckBox(tr("Degrees, minutes, seconds"))
        self.dms.setChecked(bool(self.options["dms"]))
        self.dms.toggled.connect(lambda x: self._set("dms", x))
        self.autoshape = QCheckBox(tr("Offer the whole shape (Tab takes it)"))
        self.autoshape.setToolTip(tr("After one or two sides, the shape of a neighbour of the same size - or the "
                                     "rectangle of a square corner - is offered faintly on the map."))
        self.autoshape.setChecked(bool(self.options.get("autoshape", True)))
        self.autoshape.toggled.connect(lambda x: self._set("autoshape", x))
        self.rows = {}
        for key, label, w in (("segment", tr("Segment"), self.segment), ("radius", tr("Arc radius"), self.radius),
                              ("along", tr("Points"), self.along), ("spacing", tr("Distance"), self.spacing),
                              ("parts", tr("Parts"), self.parts), ("ends", "", self.ends),
                              ("sides", tr("Sides"), self.sides), ("gap", tr("Gap tolerance"), self.gap),
                              ("chord", tr("Arc accuracy"), self.chord),
                              ("convention", tr("Directions"), self.convention), ("dms", "", self.dms),
                              ("autoshape", "", self.autoshape)):
            self.opts_form.addRow(label, w)
            self.rows[key] = w
        v.addWidget(opts)

        self.keys_hint = QLabel(tr("While drawing: L length · A direction · D deflection · X / Y coordinates "
                                   "(type the value, Tab next field, Enter apply) · P parallel / R perpendicular "
                                   "to the segment under the cursor · F2 or right click finish · Esc cancel · "
                                   "Ctrl+Z last vertex · Tab takes the suggested shape"))
        self.keys_hint.setObjectName("GpsHint")
        self.keys_hint.setWordWrap(True)
        v.addWidget(self.keys_hint)
        v.addStretch(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        self.setWidget(scroll)
        self._body = body
        self._show_options(None)

    def _spin(self, lo, hi, dec, key, tip=None):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setValue(float(self.options[key]))
        if tip:
            s.setToolTip(tip)
        s.valueChanged.connect(lambda x: self._set(key, x))
        return s

    def refresh_theme(self):
        from ..tracking import panel_qss

        self._body.setStyleSheet(panel_qss(self.plugin.theme.tokens()))

    # -- options --
    def _load_options(self):
        s = QgsSettings()
        out = dict(construct.DEFAULTS)
        for k, d in construct.DEFAULTS.items():
            val = s.value(KEY + k, d)
            try:
                out[k] = type(d)(val) if not isinstance(d, bool) else str(val).lower() in ("1", "true")
            except (TypeError, ValueError):
                out[k] = d
        return out

    def _set(self, key, value):
        self.options[key] = value
        QgsSettings().setValue(KEY + key, value)
        if self.tool is not None:
            self.tool.set_option(key, value)

    def _show_options(self, mode):
        kind = None
        if mode is not None and self.current() is not None:
            kind = construct.geometry_kind(self.current()[0])
        show = {"convention", "dms"}
        if mode in ("polyline", "polygon", "auto_complete"):
            show |= {"segment", "radius", "chord"}
        if mode == "points_along":
            show |= {"along", "spacing", "parts", "ends"}
        if mode == "polygon" and kind == construct.POLYGON:
            show.add("autoshape")
        if mode == "regular_polygon":
            show.add("sides")
        if mode in ("fill", "auto_complete"):
            show.add("gap")
        if mode in ("circle", "circle3") and kind == construct.POLYGON:
            show.add("chord")
        for key, w in self.rows.items():
            vis = key in show
            w.setVisible(vis)
            lab = self.opts_form.labelForField(w)
            if lab is not None:
                lab.setVisible(vis)

    # -- layers and templates --
    def _connect(self):
        p = QgsProject.instance()
        for sig in (p.layersAdded, p.layersRemoved, p.cleared, p.readProject):
            sig.connect(self._later_refresh)
            self._connected.append(sig)

    def _later_refresh(self, *args):
        from qgis.PyQt.QtCore import QTimer

        QTimer.singleShot(0, self.refresh)

    def layers(self):
        return [lyr for lyr in QgsProject.instance().layerTreeRoot().layerOrder() if tpl.can_create(lyr)]

    def layer_names(self):
        return [self.tree.topLevelItem(i).text(0) for i in range(self.tree.topLevelItemCount())]

    def refresh(self):
        keep = self.current()
        self.tree.clear()
        for lyr in self.layers():
            top = QTreeWidgetItem([lyr.name()])
            top.setIcon(0, QgsIconUtils.iconForLayer(lyr))
            top.setData(0, ROLE_LAYER, lyr.id())
            top.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for t in tpl.templates_or_default(lyr):
                it = QTreeWidgetItem([t.name])
                it.setIcon(0, tpl.icon_for(lyr, t, 18))
                it.setData(0, ROLE_LAYER, lyr.id())
                it.setData(0, ROLE_TEMPLATE, t.id)
                tip = t.description or ""
                if t.attributes:
                    tip += ("\n" if tip else "") + ", ".join(f"{k} = {v}" for k, v in t.attributes.items())
                it.setToolTip(0, tip)
                top.addChild(it)
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
        self._filter(self.search.text())
        if keep is not None:
            self.select_template(keep[0], keep[1].name)

    def _filter(self, text):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            lyr = QgsProject.instance().mapLayer(top.data(0, ROLE_LAYER))
            any_shown = False
            for j in range(top.childCount()):
                it = top.child(j)
                t = self._template(lyr, it.data(0, ROLE_TEMPLATE))
                ok = t is not None and tpl.matches(t, text, top.text(0))
                it.setHidden(not ok)
                any_shown |= ok
            top.setHidden(not any_shown)

    def visible_template_names(self):
        out = []
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            out += [top.child(j).text(0) for j in range(top.childCount()) if not top.child(j).isHidden()]
        return out

    def _template(self, layer, tid):
        if layer is None:
            return None
        for t in tpl.templates_or_default(layer):
            if t.id == tid:
                return t
        return None

    def current(self):
        """(layer, template) selected, or None."""
        items = self.tree.selectedItems()
        if not items or items[0].data(0, ROLE_TEMPLATE) is None:
            return None
        lyr = QgsProject.instance().mapLayer(items[0].data(0, ROLE_LAYER))
        t = self._template(lyr, items[0].data(0, ROLE_TEMPLATE))
        return (lyr, t) if t is not None else None

    def select_template(self, layer, name):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top.data(0, ROLE_LAYER) != layer.id():
                continue
            for j in range(top.childCount()):
                if top.child(j).text(0) == name:
                    self.tree.setCurrentItem(top.child(j))
                    return True
        return False

    def _on_select(self):
        cur = self.current()
        while self.tools_grid.count():
            w = self.tools_grid.takeAt(0).widget()
            if w is not None:
                self.tool_group.removeButton(w)
                w.deleteLater()
        self.tool_buttons = {}
        if cur is None:
            self.tools_title.setText(tr("Choose a template"))
            self._show_options(None)
            return
        lyr, t = cur
        self.tools_title.setText(tr("Draw “{t}” in {l}").format(t=t.name, l=lyr.name()))
        for n, (tid, label, icon) in enumerate(construct.tools_for(construct.geometry_kind(lyr))):
            b = QToolButton()
            b.setObjectName("Flat")
            b.setCheckable(True)
            b.setIcon(_icon(icon, self.plugin))
            b.setIconSize(QSize(22, 22))
            b.setToolTip(label)
            b.clicked.connect(lambda _=False, x=tid: self.start_tool(x))
            self.tool_group.addButton(b)
            self.tools_grid.addWidget(b, n // 6, n % 6)
            self.tool_buttons[tid] = b
        if self.tool is not None and self.tool.layer is lyr and self.tool.mode in self.tool_buttons:
            self.start_tool(self.tool.mode)

    def tool_ids(self):
        return list(self.tool_buttons)

    def _on_double(self, item, _col=0):
        if item.data(0, ROLE_TEMPLATE) is None:
            return
        cur = self.current()
        if cur is not None:
            self.start_tool(cur[1].tool or construct.tools_for(construct.geometry_kind(cur[0]))[0][0])

    # -- drawing --
    def start_tool(self, mode):
        cur = self.current()
        if cur is None:
            return None
        lyr, t = cur
        if not lyr.isEditable() and not lyr.startEditing():
            QMessageBox.warning(self, tr("Create Features"), tr("The layer cannot be edited."))
            return None
        canvas = self.iface.mapCanvas()
        if hasattr(self.iface, "setActiveLayer"):
            self.iface.setActiveLayer(lyr)
        cad = self.iface.cadDockWidget() if hasattr(self.iface, "cadDockWidget") else None
        old = self.tool
        self.tool = construct.ConstructTool(canvas, cad, lyr, t, mode, self.options)
        self.tool.featuresCreated.connect(self._created)
        canvas.setMapTool(self.tool)
        if old is not None:
            old.deleteLater()
        b = self.tool_buttons.get(mode)
        if b is not None:
            b.setChecked(True)
        self._show_options(mode)
        return self.tool

    def _created(self, layer, fids):
        bar = getattr(self.iface, "messageBar", None)
        if bar is not None and len(fids) > 1:
            bar().pushSuccess(tr("Create Features"), tr("{n} features added to {l}.").format(n=len(fids),
                                                                                             l=layer.name()))

    # -- template actions --
    def _layer_for_action(self):
        cur = self.current()
        if cur is not None:
            return cur[0]
        items = self.tree.selectedItems()
        lyr = QgsProject.instance().mapLayer(items[0].data(0, ROLE_LAYER)) if items else None
        if lyr is None and hasattr(self.iface, "activeLayer"):
            lyr = self.iface.activeLayer()
        return lyr if tpl.can_create(lyr) else None

    def new_template(self):
        lyr = self._layer_for_action()
        if lyr is None:
            return
        d = TemplateDialog(lyr, tpl.Template(lyr.name(), lyr.id()), self)
        if d.exec():
            t = d.result_template()
            tpl.add(lyr, t)
            self.refresh()
            self.select_template(lyr, t.name)

    def from_symbology(self):
        lyr = self._layer_for_action()
        if lyr is None:
            return
        new = tpl.make_from_symbology(lyr)
        self.refresh()
        bar = getattr(self.iface, "messageBar", None)
        if bar is not None:
            bar().pushInfo(tr("Create Features"), tr("{n} templates made from the symbology of {l}.").format(
                n=len(new), l=lyr.name()))

    def edit_template(self):
        cur = self.current()
        if cur is None:
            return
        lyr, t = cur
        d = TemplateDialog(lyr, t, self)
        if d.exec():
            nt = d.result_template()
            if nt.id == "default":
                nt.id = tpl.Template("", lyr.id()).id
            tpl.replace(lyr, nt)
            self.refresh()
            self.select_template(lyr, nt.name)

    def delete_template(self):
        cur = self.current()
        if cur is None or cur[1].id == "default":
            return
        tpl.remove(cur[0], cur[1].id)
        self.refresh()

    # -- closing --
    def shutdown(self):
        for sig in self._connected:
            try:
                sig.disconnect(self._later_refresh)
            except TypeError:
                pass
        self._connected = []
        if self.tool is not None:
            canvas = self.iface.mapCanvas()
            if canvas.mapTool() is self.tool:
                canvas.unsetMapTool(self.tool)
            self.tool.deleteLater()
            self.tool = None


def open_pane(plugin):
    d = _PANES.get(id(plugin))
    if d is None:
        d = CreateFeaturesPane(plugin)
        mw = plugin.iface.mainWindow()
        from ..commands import tab_beside

        tab_beside(mw, d, getattr(plugin, "toolbox", None))
        d.refresh_theme()
        _PANES[id(plugin)] = d
        QgsApplication.instance().aboutToQuit.connect(d.shutdown)
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

