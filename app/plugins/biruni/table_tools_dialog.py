"""The Table tools dialog: field order (table view or the data itself), field
names and aliases, highlighting presets, natural sort and type-ahead editing."""
import os

from qgis.core import QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                                 QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                                 QMessageBox, QPushButton, QRadioButton, QSpinBox, QTabWidget, QTreeWidget,
                                 QTreeWidgetItem, QVBoxLayout, QWidget)

from . import table_tools as tt
from .i18n import tr
from .version import APP_NAME

FIELDS_TAB, HIGHLIGHT_TAB, SORT_TAB = range(3)
COL_NAME, COL_NEW, COL_ALIAS, COL_TYPE = range(4)


class TableToolsDialog(QDialog):
    def __init__(self, plugin, layer, tab=FIELDS_TAB):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.layer = layer
        self.setWindowTitle(tr("Table tools") + f" - {layer.name()}")
        self.resize(640, 560)
        v = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._fields_tab(), tr("Fields order and names"))
        self.tabs.addTab(self._highlight_tab(), tr("Highlight"))
        self.tabs.addTab(self._sort_tab(), tr("Sort and typing"))
        self.tabs.setCurrentIndex(tab)
        v.addWidget(self.tabs)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _bar(self):
        return self.plugin.iface.messageBar()

    # ---- fields -----------------------------------------------------------
    def _fields_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(tr("Drag the fields into the order you want. Untick a field to hide it in the "
                              "attribute table. Type a new name or an alias (the name shown in tables and forms).")))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Field"), tr("New name"), tr("Alias"), tr("Type")])
        self.tree.setRootIsDecorated(False)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        v.addWidget(self.tree, 1)
        row = QHBoxLayout()
        for label, fn in ((tr("Up"), lambda: self._move(-1)), (tr("Down"), lambda: self._move(1)),
                          (tr("Sort A-Z"), self._sort_az), (tr("Sort by type"), self._sort_type),
                          (tr("Reset"), self._load_fields)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.view_only = QRadioButton(tr("Only in the attribute table (the data is not touched)"))
        self.in_data = QRadioButton(tr("In the data itself - a backup of the file is kept"))
        group = QButtonGroup(w)
        group.addButton(self.view_only)
        group.addButton(self.in_data)
        self.view_only.setChecked(True)
        ok, why = tt.can_rewrite(self.layer)
        self.in_data.setEnabled(ok)
        if not ok:
            self.in_data.setToolTip(why)
        v.addWidget(self.view_only)
        v.addWidget(self.in_data)
        row = QHBoxLayout()
        apply_btn = QPushButton(tr("Apply"))
        apply_btn.clicked.connect(self._apply_fields)
        self.restore_btn = QPushButton(tr("Restore a backup…"))
        self.restore_btn.clicked.connect(self._restore)
        row.addWidget(self.restore_btn)
        row.addStretch(1)
        row.addWidget(apply_btn)
        v.addLayout(row)
        self._load_fields()
        return w

    def _load_fields(self):
        self.tree.clear()
        names, hidden = tt.table_order(self.layer)
        locked = set(tt.provider_fields(self.layer)[1])
        fields = self.layer.fields()
        for name in names:
            i = fields.indexOf(name)
            f = fields.at(i)
            it = QTreeWidgetItem([name, "", self.layer.attributeAlias(i), f.typeName() or f.friendlyTypeString()])
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
            if name not in locked:
                flags |= Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsEditable
            it.setFlags(flags)  # no ItemIsDropEnabled: items cannot nest
            it.setCheckState(COL_NAME, Qt.CheckState.Unchecked if name in hidden else Qt.CheckState.Checked)
            it.setData(COL_NAME, Qt.ItemDataRole.UserRole, name)
            self.tree.addTopLevelItem(it)
        self.tree.resizeColumnToContents(COL_NAME)
        self.restore_btn.setEnabled(bool(tt.backups(self.layer)))

    def _items(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def _move(self, step):
        rows = sorted(self.tree.indexOfTopLevelItem(i) for i in self.tree.selectedItems())
        if not rows:
            return
        if step > 0:
            rows.reverse()
        for r in rows:
            t = r + step
            if 0 <= t < self.tree.topLevelItemCount():
                it = self.tree.takeTopLevelItem(r)
                self.tree.insertTopLevelItem(t, it)
                it.setSelected(True)

    def _reorder(self, key):
        items = [self.tree.takeTopLevelItem(0) for _ in range(self.tree.topLevelItemCount())]
        locked = set(tt.provider_fields(self.layer)[1])
        first = [i for i in items if i.data(COL_NAME, Qt.ItemDataRole.UserRole) in locked]
        rest = sorted((i for i in items if i not in first), key=key)
        for it in first + rest:
            self.tree.addTopLevelItem(it)

    def _sort_az(self):
        self._reorder(lambda it: tt.natural_key_value(it.text(COL_NAME)))

    def _sort_type(self):
        self._reorder(lambda it: (it.text(COL_TYPE).lower(), it.text(COL_NAME).lower()))

    def _apply_fields(self):
        items = self._items()
        order = [it.data(COL_NAME, Qt.ItemDataRole.UserRole) for it in items]
        hidden = {n for n, it in zip(order, items) if it.checkState(COL_NAME) == Qt.CheckState.Unchecked}
        renames = {n: it.text(COL_NEW).strip() for n, it in zip(order, items) if it.text(COL_NEW).strip()}
        aliases = {n: it.text(COL_ALIAS).strip() for n, it in zip(order, items)}
        if self.view_only.isChecked():
            if renames:
                QMessageBox.information(self, APP_NAME, tr("Renaming changes the data: choose \"In the data "
                                                           "itself\" to rename fields."))
                return
            tt.set_aliases(self.layer, aliases)
            tt.view_order(self.layer, order, hidden)
            self._bar().pushSuccess(APP_NAME, tr("The attribute table shows the fields in the new order."))
            return
        if QMessageBox.question(self, APP_NAME, tr("Rewrite the fields of {l} in this order? A copy of the file "
                                                   "is kept first.").format(l=self.layer.name())) \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            res = tt.rewrite_fields(self.layer, order, renames)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, tr("The fields could not be rewritten: {e}").format(e=e))
            self._load_fields()
            return
        final = [res.renamed.get(n, n) for n in order]
        tt.set_aliases(self.layer, {res.renamed.get(n, n): a for n, a in aliases.items()})
        tt.view_order(self.layer, final, {res.renamed.get(n, n) for n in hidden})
        msg = tr("The fields of {l} were rewritten in the new order.").format(l=self.layer.name())
        if res.backup and not res.backup.get("memory"):
            msg += " " + tr("Backup: {p}").format(p=res.backup["dir"])
        if res.dropped:
            msg += " " + tr("Rules of the data not carried over: {r}").format(r=", ".join(res.dropped))
        if res.renamed:
            msg += " " + tr("Labels, styles and expressions that use the old names need the new ones.")
        self._bar().pushSuccess(APP_NAME, msg)
        self._load_fields()

    def _restore(self):
        items = list(reversed(tt.backups(self.layer)))
        if not items:
            return
        labels = [f"{b['time']}  -  {os.path.basename(b['dir'])}" for b in items]
        label, ok = QInputDialog.getItem(self, tr("Restore a backup…"), tr("Put the layer back as it was on:"),
                                         labels, 0, False)
        if not ok:
            return
        try:
            tt.restore_backup(self.layer, items[labels.index(label)])
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        self._bar().pushSuccess(APP_NAME, tr("The layer was put back from the backup."))
        self._load_fields()

    # ---- highlight --------------------------------------------------------
    def _highlight_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        self.hl_field = QComboBox()
        for f in self.layer.fields():
            self.hl_field.addItem(f.displayName(), f.name())
        self.hl_rule = QComboBox()
        for key, label in (("empty", tr("Empty cells (in red)")), ("dup", tr("Values used more than once")),
                           ("range", tr("Values outside a range")), ("scale", tr("Colour scale (numbers)")),
                           ("rows", tr("Colour whole rows by this field's value"))):
            self.hl_rule.addItem(label, key)
        self.hl_min = QLineEdit()
        self.hl_max = QLineEdit()
        self.hl_steps = QSpinBox()
        self.hl_steps.setRange(2, 5)
        self.hl_steps.setValue(5)
        form.addRow(tr("Field"), self.hl_field)
        form.addRow(tr("Highlight"), self.hl_rule)
        form.addRow(tr("Minimum"), self.hl_min)
        form.addRow(tr("Maximum"), self.hl_max)
        form.addRow(tr("Colours in the scale"), self.hl_steps)
        row = QHBoxLayout()
        apply_btn = QPushButton(tr("Apply"))
        apply_btn.clicked.connect(self._apply_highlight)
        clear = QPushButton(tr("Clear Biruni highlighting"))
        clear.clicked.connect(self._clear_highlight)
        row.addWidget(clear)
        row.addStretch(1)
        row.addWidget(apply_btn)
        form.addRow(row)
        self.hl_list = QListWidget()
        form.addRow(self.hl_list)
        self._list_highlight()
        return w

    def _number(self, edit):
        t = edit.text().strip().replace(",", ".")
        if not t:
            return None
        from .arabic_text import parse_number

        return parse_number(t)

    def _apply_highlight(self):
        field = self.hl_field.currentData()
        rule = self.hl_rule.currentData()
        try:
            if rule == "empty":
                tt.highlight_empty(self.layer, [field])
            elif rule == "dup":
                tt.highlight_duplicates(self.layer, field)
            elif rule == "range":
                tt.highlight_range(self.layer, field, self._number(self.hl_min), self._number(self.hl_max))
            elif rule == "scale":
                tt.colour_scale(self.layer, field, self.hl_steps.value())
            else:
                n, more = tt.colour_rows_by(self.layer, field)
                if more:
                    self._bar().pushInfo(APP_NAME, tr("Only the first {n} values got a colour.").format(n=n))
        except ValueError as e:
            QMessageBox.information(self, APP_NAME, str(e))
            return
        self.layer.triggerRepaint()
        self._list_highlight()
        self._bar().pushSuccess(APP_NAME, tr("Open the attribute table to see the colours."))

    def _clear_highlight(self):
        tt.clear_highlighting(self.layer)
        self._list_highlight()

    def _list_highlight(self):
        self.hl_list.clear()
        for field, name in tt.highlighting(self.layer):
            QListWidgetItem(f"{field or tr('Whole rows')}: {name[len(tt.STYLE_TAG):]}", self.hl_list)

    # ---- sort and typing --------------------------------------------------
    def _sort_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        form = QFormLayout()
        self.sort_field = QComboBox()
        for f in self.layer.fields():
            self.sort_field.addItem(f.displayName(), f.name())
        self.sort_desc = QComboBox()
        self.sort_desc.addItem(tr("Ascending (2 before 10)"), False)
        self.sort_desc.addItem(tr("Descending"), True)
        form.addRow(tr("Sort the table by"), self.sort_field)
        form.addRow(tr("Order"), self.sort_desc)
        v.addLayout(form)
        b = QPushButton(tr("Sort naturally"))
        b.clicked.connect(self._sort)
        v.addWidget(b, 0, Qt.AlignmentFlag.AlignLeft)
        v.addSpacing(12)
        v.addWidget(QLabel(tr("Type-ahead: while editing, these fields offer the values already used (a new value "
                              "can still be typed).")))
        self.ac_list = QListWidget()
        current = set(tt.autocomplete_fields(self.layer))
        for f in self.layer.fields():
            it = QListWidgetItem(f.displayName(), self.ac_list)
            it.setData(Qt.ItemDataRole.UserRole, f.name())
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if f.name() in current else Qt.CheckState.Unchecked)
        v.addWidget(self.ac_list, 1)
        b2 = QPushButton(tr("Apply"))
        b2.clicked.connect(self._apply_autocomplete)
        v.addWidget(b2, 0, Qt.AlignmentFlag.AlignRight)
        return w

    def _sort(self):
        tt.sort_naturally(self.layer, self.sort_field.currentData(), self.sort_desc.currentData())
        self._bar().pushSuccess(APP_NAME, tr("The attribute table of {l} is sorted naturally. Reopen it if it is "
                                             "already open.").format(l=self.layer.name()))

    def _apply_autocomplete(self):
        on, off = [], []
        for i in range(self.ac_list.count()):
            it = self.ac_list.item(i)
            (on if it.checkState() == Qt.CheckState.Checked else off).append(it.data(Qt.ItemDataRole.UserRole))
        current = set(tt.autocomplete_fields(self.layer))
        tt.enable_autocomplete(self.layer, [n for n in on if n not in current])
        tt.disable_autocomplete(self.layer, [n for n in off if n in current])
        self._bar().pushSuccess(APP_NAME, tr("Type-ahead is on for {n} fields.").format(n=len(on)))


def open_table_tools(plugin, tab=FIELDS_TAB):
    layer = plugin.iface.activeLayer()
    if not isinstance(layer, QgsVectorLayer):
        plugin.iface.messageBar().pushInfo(APP_NAME, tr("Select a vector layer in the Layers panel first."))
        return
    TableToolsDialog(plugin, layer, tab).exec()
