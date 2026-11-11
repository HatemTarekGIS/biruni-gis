"""The Data Charter window: one place per layer, sections in a side list.

Fields · Value lists · Kinds · Rules · Linked choices · Learn from the data · Health · Dictionary and sharing ·
Change log. Edits are kept in the window until Apply writes them (fields of the data, then the layer's settings).
"""
import json
import os

from qgis.core import Qgis, QgsProject, QgsSettings, QgsVectorLayer
from qgis.PyQt.QtCore import QSize, Qt, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                                 QFileDialog, QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                                 QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter, QStackedWidget,
                                 QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..i18n import tr
from ..version import APP_NAME
from . import model

ROLE = Qt.ItemDataRole.UserRole
EDITABLE = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable
CHECKABLE = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
READONLY = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


def section_names():
    """(key, label, icon) of the sections, in order."""
    return (("fields", tr("Fields"), "columns-3"), ("lists", tr("Value lists"), "list-details"),
            ("kinds", tr("Kinds"), "category"), ("rules", tr("Rules"), "list-check"),
            ("links", tr("Linked choices"), "list-tree"), ("learn", tr("Learn from the data"), "sparkles"),
            ("health", tr("Health"), "heart-rate-monitor"), ("share", tr("Dictionary and sharing"), "book"),
            ("log", tr("Change log"), "history"))


def type_names():
    return (("text", tr("Text")), ("integer", tr("Whole number")), ("big", tr("Big whole number")),
            ("decimal", tr("Decimal number")), ("date", tr("Date")), ("datetime", tr("Date and time")),
            ("boolean", tr("Yes / No")))


def charter_qss(t):
    from ..widgets import panel_qss

    return panel_qss(t, "BiruniCharter") + f"""
#BiruniCharter QListWidget#Sections {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 8px;
    padding: 4px; outline: none; }}
#BiruniCharter QListWidget#Sections::item {{ padding: 7px 8px; border-radius: 6px; color: {t['text']}; }}
#BiruniCharter QListWidget#Sections::item:selected {{ background: {t['accent_soft']}; color: {t['text']};
    font-weight: 600; }}
#BiruniCharter QListWidget#Sections::item:hover:!selected {{ background: {t['hover']}; }}
#BiruniCharter QLabel#PageTitle {{ font-size: 13pt; font-weight: 700; color: {t['text']}; }}
#BiruniCharter QPushButton#Badge {{ border: 1px solid {t['border']}; border-radius: 11px; padding: 3px 12px;
    font-weight: 600; background: {t['panel']}; }}
#BiruniCharter QLabel#Score {{ font-size: 30pt; font-weight: 700; }}
#BiruniCharter QTableWidget {{ gridline-color: {t['border']}; }}
"""


def _item(text, flags=EDITABLE, data=None, tip="", arabic=False):
    it = QTableWidgetItem("" if text is None else str(text))
    it.setFlags(flags)
    if arabic:  # Arabic columns read from the right, whatever the interface language
        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute | Qt.AlignmentFlag.AlignVCenter)
    if data is not None:
        it.setData(ROLE, data)
    if tip:
        it.setToolTip(tip)
    return it


def _check_item(on, enabled=True):
    it = QTableWidgetItem()
    it.setFlags(CHECKABLE if enabled else Qt.ItemFlag.ItemIsSelectable)
    it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
    return it


def _checked(it):
    return it is not None and it.checkState() == Qt.CheckState.Checked


def _text(table, r, c):
    it = table.item(r, c)
    return it.text().strip() if it is not None else ""


def _split(text):
    return [s.strip() for s in str(text or "").replace("،", ",").split(",") if s.strip()]


def _table(cols, stretch=()):
    t = QTableWidget(0, len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setAlternatingRowColors(True)
    h = t.horizontalHeader()
    h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    for c in stretch:
        h.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
    return t


def _buttons(*specs):
    row = QHBoxLayout()
    out = []
    for text, fn in specs:
        b = QPushButton(text)
        b.clicked.connect(fn)
        row.addWidget(b)
        out.append(b)
    row.addStretch(1)
    return row, out


def _hint(text):
    from ..widgets import hint

    return hint(text)


def _move_row(table, step):
    r = table.currentRow()
    to = r + step
    if r < 0 or to < 0 or to >= table.rowCount():
        return
    for c in range(table.columnCount()):
        a, b = table.takeItem(r, c), table.takeItem(to, c)
        wa, wb = table.cellWidget(r, c), table.cellWidget(to, c)
        table.setItem(r, c, b)
        table.setItem(to, c, a)
        if wa is not None or wb is not None:
            # widgets cannot be swapped: copy their state
            sa = _widget_state(wa)
            _set_widget_state(wa, _widget_state(wb))
            _set_widget_state(wb, sa)
    table.setCurrentCell(to, 0)


def _widget_state(w):
    if isinstance(w, QComboBox):
        return ("combo", w.currentIndex(), w.currentText(), w.isEnabled())
    return None


def _set_widget_state(w, state):
    if isinstance(w, QComboBox) and state:
        _kind, idx, text, enabled = state
        if w.isEditable():
            w.setEditText(text)
        if idx >= 0:
            w.setCurrentIndex(idx)
        w.setEnabled(enabled)


class Page(QWidget):
    key = ""

    def __init__(self, win, title, intro):
        super().__init__()
        self.win = win
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(0, 0, 0, 0)
        self.v.setSpacing(8)
        lab = QLabel(title)
        lab.setObjectName("PageTitle")
        self.v.addWidget(lab)
        if intro:
            self.v.addWidget(_hint(intro))

    @property
    def charter(self):
        return self.win.charter

    @property
    def lists(self):
        return self.win.lists

    @property
    def layer(self):
        return self.win.layer

    def load(self):
        pass

    def save(self):
        pass


# ---- Fields -------------------------------------------------------------------------------------------------------
class FieldsPage(Page):
    key = "fields"

    def __init__(self, win):
        super().__init__(win, tr("Fields"), tr("Name, Arabic and English names (the form and the labels follow the "
                                                "interface language), type, required, automatic value, unit, value "
                                                "list and description. Apply adds, renames, deletes and orders the "
                                                "columns of the data safely (a backup copy is made first)."))
        self.table = _table([tr("Name"), tr("Arabic name"), tr("English name"), tr("Type"), tr("Length"),
                             tr("Required"), tr("Unique"), tr("Automatic value"), tr("Default"), tr("Unit"),
                             tr("Value list"), tr("Description")], stretch=(11,))
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(11, QHeaderView.ResizeMode.Stretch)
        for c, width in enumerate((110, 110, 110, 120, 54, 64, 56, 150, 76, 76, 130)):
            self.table.setColumnWidth(c, width)
        self.v.addWidget(self.table, 1)
        row, _b = _buttons((tr("Add field"), self.add_field), (tr("Delete field"), self.delete_field),
                           (tr("Move up"), lambda: _move_row(self.table, -1)),
                           (tr("Move down"), lambda: _move_row(self.table, 1)))
        self.rebuild = QPushButton(tr("Rebuild into GeoPackage…"))
        self.rebuild.clicked.connect(self.win.rebuild_layer)
        row.addWidget(self.rebuild)
        self.v.addLayout(row)
        self.note = _hint("")
        self.v.addWidget(self.note)

    def _auto_combo(self, spec):
        from .. import rules as attr_rules

        cb = QComboBox()
        for key, (label, _e, _u) in attr_rules.AUTO.items():
            cb.addItem(tr(label), key)
        cb.addItem(tr("Custom expression…"), "custom")
        cb.setCurrentIndex(max(0, cb.findData(spec.get("auto") or "")))
        return cb

    def _list_combo(self, spec):
        cb = QComboBox()
        cb.addItem("—", "")
        for lid, lst in sorted(self.lists.items(), key=lambda kv: model.pick(kv[1]) or kv[0]):
            cb.addItem(model.pick(lst) or lid, lid)
        cb.setCurrentIndex(max(0, cb.findData(spec.get("list") or "")))
        return cb

    def _unit_combo(self, spec):
        cb = QComboBox()
        cb.setEditable(True)
        cb.addItems(list(model.UNITS))
        cb.setEditText(spec.get("unit") or "")
        return cb

    def _type_combo(self, spec, new):
        cb = QComboBox()
        for key, label in type_names():
            cb.addItem(label, key)
        cb.setCurrentIndex(max(0, cb.findData(spec.get("type") or "text")))
        cb.setEnabled(new)
        if not new:
            cb.setToolTip(tr("The type of an existing column cannot change here: add a new field and copy the values "
                             "with the field calculator."))
        return cb

    def _add_row(self, spec):
        r = self.table.rowCount()
        self.table.insertRow(r)
        pk = self.win.is_key(spec.get("orig") or "")
        new = not spec.get("orig")
        self.table.setItem(r, 0, _item(spec["name"], READONLY if pk else EDITABLE, spec.get("orig") or "",
                                       tr("The layer's key") if pk else ""))
        self.table.setItem(r, 1, _item(spec.get("alias_ar"), arabic=True))
        self.table.setItem(r, 2, _item(spec.get("alias_en")))
        self.table.setCellWidget(r, 3, self._type_combo(spec, new))
        self.table.setItem(r, 4, _item(spec.get("length") or "", EDITABLE if new else READONLY))
        self.table.setItem(r, 5, _check_item(spec.get("required"), not pk))
        self.table.setItem(r, 6, _check_item(spec.get("unique"), not pk))
        auto = self._auto_combo(spec)
        auto.setEnabled(not pk)
        self.table.setCellWidget(r, 7, auto)
        self.table.setItem(r, 8, _item(spec.get("default"), READONLY if pk else EDITABLE,
                                       tip=tr("A fixed value for new features, or the expression when the automatic "
                                              "value is a custom expression.")))
        self.table.setCellWidget(r, 9, self._unit_combo(spec))
        lc = self._list_combo(spec)
        lc.setEnabled(not pk)
        self.table.setCellWidget(r, 10, lc)
        self.table.setItem(r, 11, _item(spec.get("desc")))

    def load(self):
        self.table.setRowCount(0)
        for spec in self.charter["fields"]:
            self._add_row(spec)
        ok, why = self.win.alter_state()
        self.rebuild.setVisible(not ok)
        self.note.setText("" if ok else why)

    def save(self):
        old = {f.get("orig") or "": f for f in self.charter["fields"] if f.get("orig")}
        fields = []
        for r in range(self.table.rowCount()):
            orig = self.table.item(r, 0).data(ROLE) or ""
            spec = dict(old.get(orig) or model.field_spec("", orig=""))
            spec["name"] = _text(self.table, r, 0) or orig
            spec["orig"] = orig
            spec["alias_ar"], spec["alias_en"] = _text(self.table, r, 1), _text(self.table, r, 2)
            spec["type"] = self.table.cellWidget(r, 3).currentData()
            try:
                spec["length"] = int(_text(self.table, r, 4) or 0)
            except ValueError:
                spec["length"] = 0
            spec["required"], spec["unique"] = _checked(self.table.item(r, 5)), _checked(self.table.item(r, 6))
            spec["auto"] = self.table.cellWidget(r, 7).currentData() or ""
            spec["default"] = _text(self.table, r, 8)
            spec["unit"] = self.table.cellWidget(r, 9).currentText().strip()
            spec["list"] = self.table.cellWidget(r, 10).currentData() or ""
            spec["desc"] = _text(self.table, r, 11)
            if spec["name"]:
                fields.append(spec)
        self.charter["fields"] = fields

    def add_field(self):
        self.save()
        taken = {f["name"] for f in self.charter["fields"]}
        n, name = 1, "new_field"
        while name in taken:
            n += 1
            name = f"new_field_{n}"
        spec = model.field_spec(name, orig="")
        self.charter["fields"].append(spec)
        self._add_row(spec)
        self.table.setCurrentCell(self.table.rowCount() - 1, 0)
        self.table.editItem(self.table.item(self.table.rowCount() - 1, 0))

    def delete_field(self):
        r = self.table.currentRow()
        if r < 0:
            return
        orig = self.table.item(r, 0).data(ROLE) or ""
        if orig and self.win.is_key(orig):
            return
        if orig:
            if self.win.ask and QMessageBox.question(
                    self, APP_NAME, tr("Delete the field {f} and all its values when you press Apply?").format(f=orig)
            ) != QMessageBox.StandardButton.Yes:
                return
            self.win.deleted.append(orig)
        self.table.removeRow(r)
        self.save()


# ---- Value lists ---------------------------------------------------------------------------------------------------
class ListsPage(Page):
    key = "lists"

    def __init__(self, win):
        super().__init__(win, tr("Value lists"), tr("Shared lists of codes with Arabic and English labels (and a "
                                                     "colour), or a range of numbers. One list can serve many fields and "
                                                     "layers of the project; pick it in the Fields section."))
        split = QSplitter()
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.names = QListWidget()
        self.names.currentItemChanged.connect(self._switch)
        lv.addWidget(self.names, 1)
        row, _b = _buttons((tr("New list"), lambda: self.new_list("codes")),
                           (tr("New range"), lambda: self.new_list("range")))
        lv.addLayout(row)
        row, _b = _buttons((tr("From a field…"), self.from_field), (tr("Delete list"), self.delete_list))
        lv.addLayout(row)
        split.addWidget(left)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 0, 0, 0)
        form = QFormLayout()
        self.ar, self.en = QLineEdit(), QLineEdit()
        self.ar.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        form.addRow(tr("Arabic name"), self.ar)
        form.addRow(tr("English name"), self.en)
        self.lo, self.hi, self.step = QLineEdit(), QLineEdit(), QLineEdit()
        self.range_box = QWidget()
        rf = QFormLayout(self.range_box)
        rf.setContentsMargins(0, 0, 0, 0)
        rf.addRow(tr("Minimum"), self.lo)
        rf.addRow(tr("Maximum"), self.hi)
        rf.addRow(tr("Step"), self.step)
        rv.addLayout(form)
        rv.addWidget(self.range_box)
        self.items = _table([tr("Code"), tr("Arabic label"), tr("English label"), tr("Colour")], stretch=(1, 2))
        self.items.cellDoubleClicked.connect(self._pick_colour)
        rv.addWidget(self.items, 1)
        row, self.item_buttons = _buttons((tr("Add value"), self.add_item), (tr("Remove value"), self.remove_item),
                                          (tr("Move up"), lambda: _move_row(self.items, -1)),
                                          (tr("Move down"), lambda: _move_row(self.items, 1)))
        rv.addLayout(row)
        self.used = _hint("")
        rv.addWidget(self.used)
        split.addWidget(right)
        split.setSizes([220, 640])
        self.v.addWidget(split, 1)
        self.current = None

    def load(self):
        self.current = None
        self.names.blockSignals(True)
        self.names.clear()
        for lid, lst in sorted(self.lists.items(), key=lambda kv: model.pick(kv[1]) or kv[0]):
            it = QListWidgetItem(self._title(lst))
            it.setData(ROLE, lid)
            self.names.addItem(it)
        self.names.blockSignals(False)
        if self.names.count():
            self.names.setCurrentRow(0)
        else:
            self._show(None)

    @staticmethod
    def _title(lst):
        n = len(lst["items"]) if lst["kind"] == "codes" else None
        name = model.pick(lst) or lst["id"]
        return f"{name}  ({n})" if n is not None else f"{name}  ({tr('range')})"

    def _switch(self, cur, _prev):
        self.save()
        self._show(cur.data(ROLE) if cur is not None else None)

    def _show(self, lid):
        self.current = lid
        lst = self.lists.get(lid) if lid else None
        for w in (self.ar, self.en, self.items, self.range_box):
            w.setEnabled(lst is not None)
        if lst is None:
            self.ar.clear()
            self.en.clear()
            self.items.setRowCount(0)
            self.used.setText("")
            return
        self.ar.setText(lst.get("ar") or "")
        self.en.setText(lst.get("en") or "")
        codes = lst["kind"] == "codes"
        self.range_box.setVisible(not codes)
        self.items.setVisible(codes)
        for b in self.item_buttons:
            b.setVisible(codes)
        self.lo.setText("" if lst.get("min") is None else f"{float(lst['min']):g}")
        self.hi.setText("" if lst.get("max") is None else f"{float(lst['max']):g}")
        self.step.setText("" if lst.get("step") is None else f"{float(lst['step']):g}")
        self.items.setRowCount(0)
        for it in lst["items"]:
            self._add_item_row(it)
        used = sorted(f"{lyr.name()}.{f}" for lyr, f in model.list_usages(lid))
        here = [f["name"] for f in self.charter["fields"] if f.get("list") == lid]
        used = sorted(set(used) | {f"{self.layer.name()}.{f}" for f in here} if self.layer else set(used))
        self.used.setText(tr("Used by: {u}").format(u=", ".join(used)) if used else tr("Not used yet."))

    def _add_item_row(self, it):
        r = self.items.rowCount()
        self.items.insertRow(r)
        self.items.setItem(r, 0, _item(it.get("code")))
        self.items.setItem(r, 1, _item(it.get("ar"), arabic=True))
        self.items.setItem(r, 2, _item(it.get("en")))
        col = _item(it.get("color") or "", READONLY, tip=tr("Double-click to choose a colour"))
        if it.get("color"):
            col.setBackground(QColor(it["color"]))
        self.items.setItem(r, 3, col)

    def _pick_colour(self, r, c):
        if c != 3:
            return
        cur = QColor(_text(self.items, r, 3) or "#0E7C74")
        col = QColorDialog.getColor(cur, self, tr("Colour"))
        if col.isValid():
            it = self.items.item(r, 3)
            it.setText(col.name())
            it.setBackground(col)

    def save(self):
        lst = self.lists.get(self.current) if self.current else None
        if lst is None:
            return
        lst["ar"], lst["en"] = self.ar.text().strip(), self.en.text().strip()
        if lst["kind"] == "codes":
            items = []
            for r in range(self.items.rowCount()):
                code = _text(self.items, r, 0)
                if code:
                    items.append({"code": code, "ar": _text(self.items, r, 1), "en": _text(self.items, r, 2),
                                  "color": _text(self.items, r, 3)})
            lst["items"] = items
        else:
            for key, w in (("min", self.lo), ("max", self.hi), ("step", self.step)):
                try:
                    lst[key] = float(w.text().replace(",", ".")) if w.text().strip() else None
                except ValueError:
                    pass
        cur = self.names.currentItem()
        if cur is not None and cur.data(ROLE) == self.current:
            cur.setText(self._title(lst))

    def new_list(self, kind, items=(), ar="", en=""):
        self.save()
        base = model.slug(en or ar or (tr("New list") if kind == "codes" else tr("New range")))
        lid = model.unique_list_id(base, self.lists)
        self.lists[lid] = model.new_list(ar or (tr("New list") if kind == "codes" and model.ui_lang() == "ar" else ""),
                                         en or (lid if model.ui_lang() == "en" else ""), kind, items, lid=lid)
        if model.ui_lang() == "ar" and not ar:
            self.lists[lid]["ar"] = tr("New list") if kind == "codes" else tr("New range")
        self.load()
        for i in range(self.names.count()):
            if self.names.item(i).data(ROLE) == lid:
                self.names.setCurrentRow(i)
        return lid

    def from_field(self):
        if self.layer is None:
            return
        from qgis.PyQt.QtWidgets import QInputDialog

        names = [f.name() for f in self.layer.fields()]
        name, ok = QInputDialog.getItem(self, tr("Value list from a field"), tr("Field"), names, 0, False)
        if ok and name:
            self.list_from_field(name)

    def list_from_field(self, name):
        from collections import Counter

        idx = self.layer.fields().indexOf(name)
        counts = Counter(str(v) for v in self.layer.uniqueValues(idx) if v is not None and str(v) != "NULL")
        items = []
        for v in sorted(counts):
            it = {"code": v, "ar": "", "en": ""}
            it["ar" if any("؀" <= ch <= "ۿ" for ch in v) else "en"] = v
            items.append(it)
        spec = model.find_field(self.charter, name) or {}
        lid = self.new_list("codes", items, ar=spec.get("alias_ar") or (name if model.ui_lang() == "ar" else ""),
                            en=spec.get("alias_en") or name)
        if spec and not spec.get("list"):
            spec["list"] = lid
        return lid

    def delete_list(self):
        if not self.current:
            return
        used = model.list_usages(self.current)
        if used and self.win.ask and QMessageBox.question(
                self, APP_NAME, tr("{n} fields use this list. Delete it anyway?").format(n=len(used))
        ) != QMessageBox.StandardButton.Yes:
            return
        self.lists.pop(self.current, None)
        for f in self.charter["fields"]:
            if f.get("list") == self.current:
                f["list"] = ""
        self.current = None
        self.load()

    def add_item(self):
        self._add_item_row({})
        self.items.setCurrentCell(self.items.rowCount() - 1, 0)
        self.items.editItem(self.items.item(self.items.rowCount() - 1, 0))

    def remove_item(self):
        if self.items.currentRow() >= 0:
            self.items.removeRow(self.items.currentRow())


# ---- Kinds ---------------------------------------------------------------------------------------------------------
class KindsPage(Page):
    key = "kinds"

    def __init__(self, win):
        super().__init__(win, tr("Kinds"), tr("Choose the field that says what kind each feature is. Each kind can "
                                               "have its own default values, its own allowed values per field, its "
                                               "own required fields and its own symbol - in every edit form and in "
                                               "QField."))
        top = QHBoxLayout()
        top.addWidget(QLabel(tr("Kind field")))
        self.field = QComboBox()
        self.field.activated.connect(self._field_changed)
        top.addWidget(self.field, 1)
        self.symbols = QCheckBox(tr("A symbol per kind on the map"))
        top.addWidget(self.symbols)
        self.v.addLayout(top)
        split = QSplitter(Qt.Orientation.Vertical)
        up = QWidget()
        uv = QVBoxLayout(up)
        uv.setContentsMargins(0, 0, 0, 0)
        self.kinds = _table([tr("Code"), tr("Arabic name"), tr("English name"), tr("Colour")], stretch=(1, 2))
        self.kinds.currentCellChanged.connect(self._kind_changed)
        self.kinds.cellDoubleClicked.connect(self._pick_colour)
        uv.addWidget(self.kinds, 1)
        row, _b = _buttons((tr("Add kind"), self.add_kind), (tr("Remove kind"), self.remove_kind),
                           (tr("From the field's values"), self.from_values))
        uv.addLayout(row)
        split.addWidget(up)
        down = QWidget()
        dv = QVBoxLayout(down)
        dv.setContentsMargins(0, 0, 0, 0)
        self.for_kind = QLabel()
        self.for_kind.setObjectName("Title")
        dv.addWidget(self.for_kind)
        self.rules = _table([tr("Field"), tr("Default"), tr("Allowed values (codes, separated by commas)"),
                             tr("Required")], stretch=(2,))
        dv.addWidget(self.rules, 1)
        split.addWidget(down)
        self.v.addWidget(split, 1)
        self.current = -1

    def load(self):
        self.current = -1
        self.field.clear()
        self.field.addItem("—", "")
        for f in self.charter["fields"]:
            if not self.win.is_key(f["name"]):
                self.field.addItem(f["name"], f["name"])
        self.field.setCurrentIndex(max(0, self.field.findData(self.charter["kind_field"])))
        self.symbols.setChecked(bool(self.charter.get("kind_symbols", True)))
        self.kinds.blockSignals(True)
        self.kinds.setRowCount(0)
        for k in self.charter["kinds"]:
            self._add_kind_row(k)
        self.kinds.blockSignals(False)
        if self.kinds.rowCount():
            self.kinds.setCurrentCell(0, 0)
        self._show_rules(0 if self.kinds.rowCount() else -1)

    def _add_kind_row(self, k):
        r = self.kinds.rowCount()
        self.kinds.insertRow(r)
        self.kinds.setItem(r, 0, _item(k.get("code"), data=k))
        self.kinds.setItem(r, 1, _item(k.get("ar"), arabic=True))
        self.kinds.setItem(r, 2, _item(k.get("en")))
        col = _item(k.get("color") or "", READONLY, tip=tr("Double-click to choose a colour"))
        if k.get("color"):
            col.setBackground(QColor(k["color"]))
        self.kinds.setItem(r, 3, col)

    def _pick_colour(self, r, c):
        if c == 3:
            cur = QColor(_text(self.kinds, r, 3) or "#0E7C74")
            col = QColorDialog.getColor(cur, self, tr("Colour"))
            if col.isValid():
                self.kinds.item(r, 3).setText(col.name())
                self.kinds.item(r, 3).setBackground(col)

    def _field_changed(self, *_):
        self.save()
        self.charter["kind_field"] = self.field.currentData() or ""
        self._show_rules(self.current)

    def _kind_changed(self, row, _c, prev, _pc):
        if row == prev:
            return
        self._save_rules()
        self._show_rules(row)

    def _kind_at(self, r):
        it = self.kinds.item(r, 0) if r >= 0 else None
        return it.data(ROLE) if it is not None else None

    def _show_rules(self, r):
        self.current = r
        k = self._kind_at(r)
        self.rules.setRowCount(0)
        kf = self.field.currentData() or ""
        if k is None or not kf:
            self.for_kind.setText(tr("Choose a kind field and add kinds.") if not kf else tr("Add kinds."))
            return
        self.for_kind.setText(tr("For the kind {k}").format(k=_text(self.kinds, r, 0)))
        for f in self.charter["fields"]:
            name = f["name"]
            if name == kf or self.win.is_key(name):
                continue
            row = self.rules.rowCount()
            self.rules.insertRow(row)
            self.rules.setItem(row, 0, _item(model.field_label(f), READONLY, name, name))
            self.rules.setItem(row, 1, _item(k["defaults"].get(name, "")))
            lst = self.lists.get(f.get("list") or "")
            tip = ", ".join(model.list_codes(lst)) if lst and lst["kind"] == "codes" else ""
            self.rules.setItem(row, 2, _item(", ".join(k["lists"].get(name, [])),
                                             tip=tr("From the list: {v}").format(v=tip) if tip else ""))
            self.rules.setItem(row, 3, _check_item(name in k["required"]))

    def _save_rules(self):
        k = self._kind_at(self.current)
        if k is None or not self.rules.rowCount():
            return
        k["defaults"], k["lists"], k["required"] = {}, {}, []
        for r in range(self.rules.rowCount()):
            name = self.rules.item(r, 0).data(ROLE)
            d = _text(self.rules, r, 1)
            if d:
                k["defaults"][name] = d
            allowed = _split(_text(self.rules, r, 2))
            if allowed:
                k["lists"][name] = allowed
            if _checked(self.rules.item(r, 3)):
                k["required"].append(name)

    def save(self):
        self._save_rules()
        self.charter["kind_field"] = self.field.currentData() or ""
        self.charter["kind_symbols"] = self.symbols.isChecked()
        kinds = []
        for r in range(self.kinds.rowCount()):
            k = dict(self._kind_at(r) or {"defaults": {}, "lists": {}, "required": []})
            k["code"], k["ar"], k["en"] = _text(self.kinds, r, 0), _text(self.kinds, r, 1), _text(self.kinds, r, 2)
            k["color"] = _text(self.kinds, r, 3)
            if k["code"]:
                kinds.append(k)
                self.kinds.item(r, 0).setData(ROLE, k)
        self.charter["kinds"] = kinds

    def add_kind(self):
        self._save_rules()
        k = {"code": "", "ar": "", "en": "", "color": "", "defaults": {}, "lists": {}, "required": []}
        self._add_kind_row(k)
        r = self.kinds.rowCount() - 1
        self.kinds.setCurrentCell(r, 0)
        self.kinds.editItem(self.kinds.item(r, 0))

    def remove_kind(self):
        r = self.kinds.currentRow()
        if r >= 0:
            self.kinds.removeRow(r)
            self.current = -1
            self._show_rules(self.kinds.currentRow())

    def from_values(self):
        kf = self.field.currentData() or ""
        if not kf or self.layer is None or self.layer.fields().indexOf(kf) < 0:
            return
        self.save()
        have = {k["code"] for k in self.charter["kinds"]}
        palette = ("#0E7C74", "#D97706", "#2563EB", "#DC2626", "#7C3AED", "#059669", "#DB2777", "#65A30D")
        for v in sorted(str(x) for x in self.layer.uniqueValues(self.layer.fields().indexOf(kf))
                        if x is not None and str(x) != "NULL"):
            if v not in have:
                k = {"code": v, "ar": "", "en": "", "defaults": {}, "lists": {}, "required": [],
                     "color": palette[len(self.charter["kinds"]) % len(palette)]}
                k["ar" if any("؀" <= ch <= "ۿ" for ch in v) else "en"] = v
                self.charter["kinds"].append(k)
        self.load()


# ---- Rules ---------------------------------------------------------------------------------------------------------
class RulesPage(Page):
    key = "rules"

    def __init__(self, win):
        super().__init__(win, tr("Rules"), tr("Required, unique, ranges and automatic values are set in Fields and "
                                               "Value lists (Biruni's attribute rules). Here: rules across fields - "
                                               "an expression that must be true, with a message in Arabic and "
                                               "English. Errors stop the save in the form; warnings only show."))
        self.table = _table([tr("Rule (expression that must be true)"), tr("Arabic message"), tr("English message"),
                             tr("Level"), tr("Field"), tr("Breaking it")], stretch=(0, 1, 2))
        self.v.addWidget(self.table, 1)
        row, _b = _buttons((tr("Add rule"), self.add_rule), (tr("Remove rule"), self.remove_rule),
                           (tr("Expression…"), self.edit_expression), (tr("Count features"), self.count))
        self.v.addLayout(row)

    def _add_row(self, r):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, _item(r.get("expr"), data=r.get("id") or model.new_id("R")))
        self.table.setItem(row, 1, _item(r.get("ar"), arabic=True))
        self.table.setItem(row, 2, _item(r.get("en")))
        lv = QComboBox()
        lv.addItem(tr("Error"), "error")
        lv.addItem(tr("Warning"), "warning")
        lv.setCurrentIndex(max(0, lv.findData(r.get("level") or "error")))
        self.table.setCellWidget(row, 3, lv)
        fc = QComboBox()
        fc.addItem(tr("(first field it uses)"), "")
        for f in self.charter["fields"]:
            fc.addItem(f["name"], f["name"])
        fc.setCurrentIndex(max(0, fc.findData(r.get("field") or "")))
        self.table.setCellWidget(row, 4, fc)
        self.table.setItem(row, 5, _item("", READONLY))

    def load(self):
        self.table.setRowCount(0)
        for r in self.charter["rules"]:
            self._add_row(r)

    def save(self):
        out = []
        for row in range(self.table.rowCount()):
            expr = _text(self.table, row, 0)
            if not expr:
                continue
            out.append({"id": self.table.item(row, 0).data(ROLE), "expr": expr, "ar": _text(self.table, row, 1),
                        "en": _text(self.table, row, 2), "level": self.table.cellWidget(row, 3).currentData(),
                        "field": self.table.cellWidget(row, 4).currentData() or ""})
        self.charter["rules"] = out

    def add_rule(self):
        self._add_row({"id": model.new_id("R"), "level": "error"})
        r = self.table.rowCount() - 1
        self.table.setCurrentCell(r, 0)
        self.table.editItem(self.table.item(r, 0))

    def remove_rule(self):
        if self.table.currentRow() >= 0:
            self.table.removeRow(self.table.currentRow())

    def edit_expression(self):
        r = self.table.currentRow()
        if r < 0 or self.layer is None:
            return
        from qgis.gui import QgsExpressionBuilderDialog

        d = QgsExpressionBuilderDialog(self.layer, _text(self.table, r, 0), self)
        d.setWindowTitle(tr("Rule (expression that must be true)"))
        if d.exec():
            self.table.item(r, 0).setText(d.expressionText())

    def count(self):
        from .health import evaluate

        self.save()
        rep = evaluate(self.layer, self.charter, self.lists)
        counts = {it.id: it.count for it in rep.items}
        for row in range(self.table.rowCount()):
            n = counts.get("rule:" + str(self.table.item(row, 0).data(ROLE)), 0)
            self.table.item(row, 5).setText(str(n))
        if rep.errors:
            self.win.say("; ".join(rep.errors[:3]), error=True)
        return counts


# ---- Linked choices ------------------------------------------------------------------------------------------------
class LinksPage(Page):
    key = "links"

    def __init__(self, win):
        super().__init__(win, tr("Linked choices"), tr("Choosing a value in one field limits the choices of another: "
                                                        "governorate → district → neighbourhood, road class → speeds. "
                                                        "The form shows only the values that go with what is already "
                                                        "chosen (in Biruni and in QField)."))
        split = QSplitter()
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.names = QListWidget()
        self.names.currentRowChanged.connect(self._switch)
        lv.addWidget(self.names, 1)
        row, _b = _buttons((tr("New link"), self.new_link), (tr("Delete link"), self.delete_link))
        lv.addLayout(row)
        split.addWidget(left)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 0, 0, 0)
        form = QFormLayout()
        self.p1, self.p2, self.child = QComboBox(), QComboBox(), QComboBox()
        form.addRow(tr("When this field…"), self.p1)
        form.addRow(tr("…and this one (optional)"), self.p2)
        form.addRow(tr("…limits this field"), self.child)
        rv.addLayout(form)
        self.pairs = _table([tr("When it is"), tr("Allowed values (codes, separated by commas)")], stretch=(1,))
        rv.addWidget(self.pairs, 1)
        row, _b = _buttons((tr("Fill from the data"), self.fill_from_data), (tr("Add row"), self.add_pair),
                           (tr("Remove row"), self.remove_pair))
        rv.addLayout(row)
        rv.addWidget(_hint(tr("With two fields, write the values joined by | (e.g. Riyadh|Olaya).")))
        split.addWidget(right)
        split.setSizes([220, 640])
        self.v.addWidget(split, 1)
        self.current = -1

    def _fill_combos(self):
        for cb, empty in ((self.p1, False), (self.p2, True), (self.child, False)):
            cb.clear()
            if empty:
                cb.addItem("—", "")
            for f in self.charter["fields"]:
                if not self.win.is_key(f["name"]):
                    cb.addItem(f["name"], f["name"])

    def load(self):
        self.current = -1
        self._fill_combos()
        self.names.blockSignals(True)
        self.names.clear()
        for ln in self.charter["links"]:
            self.names.addItem(self._title(ln))
        self.names.blockSignals(False)
        if self.names.count():
            self.names.setCurrentRow(0)
        else:
            self._show(-1)

    @staticmethod
    def _title(ln):
        return " + ".join(ln.get("parents") or ["?"]) + f" {model.arrow()} " + (ln.get("child") or "?")

    def _switch(self, row):
        self._save_current()
        self._show(row)

    def _show(self, row):
        self.current = row
        ln = self.charter["links"][row] if 0 <= row < len(self.charter["links"]) else None
        for w in (self.p1, self.p2, self.child, self.pairs):
            w.setEnabled(ln is not None)
        self.pairs.setRowCount(0)
        if ln is None:
            return
        parents = ln.get("parents") or []
        self.p1.setCurrentIndex(max(0, self.p1.findData(parents[0] if parents else "")))
        self.p2.setCurrentIndex(max(0, self.p2.findData(parents[1] if len(parents) > 1 else "")))
        self.child.setCurrentIndex(max(0, self.child.findData(ln.get("child") or "")))
        for key, codes in ln["pairs"].items():
            self._add_pair_row(key, codes)

    def _add_pair_row(self, key="", codes=()):
        r = self.pairs.rowCount()
        self.pairs.insertRow(r)
        self.pairs.setItem(r, 0, _item(key))
        self.pairs.setItem(r, 1, _item(", ".join(codes)))

    def _save_current(self):
        if not 0 <= self.current < len(self.charter["links"]):
            return
        ln = self.charter["links"][self.current]
        ln["parents"] = [p for p in (self.p1.currentData(), self.p2.currentData()) if p]
        ln["child"] = self.child.currentData() or ""
        pairs = {}
        for r in range(self.pairs.rowCount()):
            key = _text(self.pairs, r, 0)
            if key:
                pairs[key] = _split(_text(self.pairs, r, 1))
        ln["pairs"] = pairs
        it = self.names.item(self.current)
        if it is not None:
            it.setText(self._title(ln))

    def save(self):
        self._save_current()
        self.charter["links"] = [ln for ln in self.charter["links"] if ln.get("child") and ln.get("parents")
                                 and ln["child"] not in ln["parents"]]

    def new_link(self):
        self._save_current()
        names = [f["name"] for f in self.charter["fields"] if not self.win.is_key(f["name"])]
        if len(names) < 2:
            return
        self.charter["links"].append({"id": model.new_id("K"), "parents": [names[0]], "child": names[1], "pairs": {}})
        self.names.addItem(self._title(self.charter["links"][-1]))
        self.names.setCurrentRow(self.names.count() - 1)

    def delete_link(self):
        r = self.names.currentRow()
        if 0 <= r < len(self.charter["links"]):
            self.current = -1
            del self.charter["links"][r]
            self.load()

    def fill_from_data(self):
        from .apply import SEP

        if self.layer is None:
            return
        parents = [p for p in (self.p1.currentData(), self.p2.currentData()) if p]
        child = self.child.currentData()
        if not parents or not child or child in parents or self.layer.fields().indexOf(child) < 0:
            return
        pairs = {}
        for f in self.layer.getFeatures():
            vals = [f[p] for p in parents]
            c = f[child]
            if any(v is None or str(v) == "NULL" for v in vals) or c is None or str(c) == "NULL":
                continue
            key = SEP.join(str(v) for v in vals)
            pairs.setdefault(key, [])
            if str(c) not in pairs[key]:
                pairs[key].append(str(c))
        self.pairs.setRowCount(0)
        for key in sorted(pairs):
            self._add_pair_row(key, sorted(pairs[key]))

    def add_pair(self):
        self._add_pair_row()

    def remove_pair(self):
        if self.pairs.currentRow() >= 0:
            self.pairs.removeRow(self.pairs.currentRow())


# ---- Learn from the data -------------------------------------------------------------------------------------------
class LearnPage(Page):
    key = "learn"

    def __init__(self, win):
        super().__init__(win, tr("Learn from the data"), tr("Biruni reads the layer and proposes a charter: value "
                                                             "lists, one spelling for each value, ranges with "
                                                             "suspicious numbers, required and unique fields, kinds "
                                                             "and linked choices. Tick what you accept."))
        row, _b = _buttons((tr("Read the data"), self.scan),)
        _b[0].setObjectName("Primary")
        self.v.addLayout(row)
        self.table = _table([tr("Accept"), tr("Proposal"), tr("Details"), tr("Confidence")], stretch=(2,))
        self.table.setWordWrap(True)
        self.table.cellClicked.connect(self._select)
        self.v.addWidget(self.table, 1)
        self.fix = QCheckBox(tr("Also fix the spellings in the data (one Undo step, not saved)"))
        self.fix.setChecked(True)
        self.v.addWidget(self.fix)
        row, self.accept_btn = _buttons((tr("Accept the ticked proposals"), self.accept),)
        self.accept_btn[0].setObjectName("Primary")
        self.v.addLayout(row)
        self.proposals = []

    def scan(self):
        from . import learn

        if self.layer is None:
            return []
        self.win.save_all()
        self.proposals = learn.infer(self.layer, self.lists)
        self.show_proposals()
        self.win.say(tr("{n} proposals.").format(n=len(self.proposals)) if self.proposals else
                     tr("Nothing to propose: the data has no clear pattern yet."))
        return self.proposals

    def show_proposals(self):
        labels = {"list": tr("Value list"), "merge": tr("Spelling"), "range": tr("Range"), "required": tr("Required"),
                  "unique": tr("Unique"), "kind": tr("Kind"), "link": tr("Linked choice")}
        self.table.setRowCount(0)
        for p in self.proposals:
            r = self.table.rowCount()
            self.table.insertRow(r)
            it = _check_item(p.checked)
            it.setText(labels.get(p.kind, p.kind))
            self.table.setItem(r, 0, it)
            self.table.setItem(r, 1, _item(p.title, READONLY))
            self.table.setItem(r, 2, _item(p.detail, READONLY, tip=p.detail))
            self.table.setItem(r, 3, _item(f"{round(100 * p.confidence)}%", READONLY))
        self.table.resizeRowsToContents()

    def _select(self, r, _c):
        if 0 <= r < len(self.proposals) and self.proposals[r].fids and self.layer is not None:
            self.layer.selectByIds(self.proposals[r].fids)

    def accept(self):
        from . import learn

        for r, p in enumerate(self.proposals):
            p.checked = _checked(self.table.item(r, 0))
        if not any(p.checked for p in self.proposals):
            return
        self.win.save_all()
        charter, lists, changed = learn.accept(self.layer, self.proposals, self.charter, self.lists,
                                               self.fix.isChecked())
        self.win.charter, self.win.lists = charter, lists
        self.win.apply(section="learn", quiet=True)
        msg = tr("The proposals were applied to the charter.")
        if changed:
            msg += " " + tr("{n} features got one spelling - Undo (Ctrl+Z) takes them back; save the layer to keep "
                            "them.").format(n=changed)
        self.win.say(msg)
        self.proposals = []
        self.table.setRowCount(0)


# ---- Health --------------------------------------------------------------------------------------------------------
class HealthPage(Page):
    key = "health"

    def __init__(self, win):
        super().__init__(win, tr("Health"), tr("How many features break which rule of the charter. It follows your "
                                                "edits; click a row to select those features."))
        top = QHBoxLayout()
        self.score = QLabel("–")
        self.score.setObjectName("Score")
        top.addWidget(self.score)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        top.addWidget(self.summary, 1)
        self.v.addLayout(top)
        self.table = _table([tr("Problem"), tr("Field"), tr("Level"), tr("Features")], stretch=(0,))
        self.table.cellClicked.connect(self._select)
        self.v.addWidget(self.table, 1)
        row, _b = _buttons((tr("Check again"), self.win.refresh_health), (tr("Zoom to them"), self._zoom))
        self.v.addLayout(row)
        self.report = None

    def show_report(self, rep):
        self.report = rep
        self.table.setRowCount(0)
        if rep is None:
            self.score.setText("–")
            self.summary.setText("")
            return
        self.score.setText(f"{rep.score}%")
        self.score.setStyleSheet(f"color: {score_colour(rep.score)};")
        self.summary.setText(tr("{e} of {n} features break a rule, {w} more only a warning.").format(
            e=rep.bad_errors, n=rep.total, w=rep.bad_warnings) + ("\n" + "; ".join(rep.errors[:2]) if rep.errors else ""))
        for it in rep.items:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, _item(it.title, READONLY))
            self.table.setItem(r, 1, _item(it.field, READONLY))
            lv = _item(tr("Error") if it.level == "error" else tr("Warning"), READONLY)
            lv.setForeground(QColor("#DC2626" if it.level == "error" else "#D97706"))
            self.table.setItem(r, 2, lv)
            self.table.setItem(r, 3, _item(it.count, READONLY))

    def _select(self, r, _c):
        if self.report and 0 <= r < len(self.report.items) and self.layer is not None:
            self.layer.selectByIds(self.report.items[r].fids)

    def _zoom(self):
        if self.layer is not None and self.layer.selectedFeatureCount() and self.win.plugin is not None:
            try:
                self.win.plugin.iface.mapCanvas().zoomToSelected(self.layer)
            except AttributeError:
                pass


def score_colour(score):
    return "#0E9F6E" if score >= 90 else ("#D97706" if score >= 70 else "#DC2626")


# ---- Dictionary and sharing ---------------------------------------------------------------------------------------
class SharePage(Page):
    key = "share"

    def __init__(self, win):
        super().__init__(win, tr("Dictionary and sharing"), "")
        from ..widgets import card, title

        f, lay = card(self.v)
        lay.addWidget(title(tr("Data dictionary")))
        lay.addWidget(_hint(tr("Every layer with a charter, its fields, names in Arabic and English, types, units, "
                               "descriptions, value lists, kinds, rules, linked choices and health - one page that "
                               "opens in Word or a browser, for a client or a supervisor.")))
        row, _b = _buttons((tr("Make the data dictionary…"), self.dictionary),)
        _b[0].setObjectName("Primary")
        lay.addLayout(row)
        f, lay = card(self.v)
        lay.addWidget(title(tr("The charter travels")))
        lay.addWidget(_hint(tr("Save the charter as a file, use it in another project, or apply it to another layer "
                               "- fields are matched by name and you can correct the match.")))
        row, _b = _buttons((tr("Export the charter…"), self.export), (tr("Import a charter…"), self.import_),
                           (tr("Apply to another layer…"), self.to_layer))
        lay.addLayout(row)
        f, lay = card(self.v)
        lay.addWidget(title(tr("Phone form")))
        self.form_info = _hint("")
        lay.addWidget(self.form_info)
        row, _b = _buttons((tr("Use as the Field Photos form"), self.use_phone_form),
                           (tr("Save the phone form…"), self.save_phone_form))
        lay.addLayout(row)
        lay.addWidget(_hint(tr("QField reads the same charter from the project: lists, kinds, linked choices and "
                               "rules work there as they do here.")))
        self.v.addStretch(1)

    def load(self):
        from .travel import phone_form

        form = phone_form(self.charter, self.lists)
        self.form_info.setText(tr("The questions of the Field Photos phone form, from this charter: {n} questions "
                                  "({c} with choices).").format(n=len(form), c=sum(1 for q in form
                                                                                   if q["type"] == "choice")))

    def _path(self, title, name, flt, save=True):
        if self.win.next_path:
            return self.win.next_path.pop(0)
        folder = QgsSettings().value("biruni/charter/folder", os.path.expanduser("~"))
        fn = QFileDialog.getSaveFileName if save else QFileDialog.getOpenFileName
        path, _ = fn(self, title, os.path.join(folder, name), flt)
        if path:
            QgsSettings().setValue("biruni/charter/folder", os.path.dirname(path))
        return path

    def dictionary(self):
        from .dictionary import write_html

        self.win.save_all()
        path = self._path(tr("Data dictionary"), (QgsProject.instance().baseName() or "data") + "_dictionary.html",
                          tr("Web page (*.html)"))
        if not path:
            return None
        layers = model.charter_layers()
        if self.layer is not None and self.layer not in layers:
            layers.append(self.layer)
        write_html(path, layers)
        self.win.say(tr("Saved: {p}").format(p=path))
        if self.win.ask:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        return path

    def export(self):
        from .travel import export_charter

        self.win.save_all()
        path = self._path(tr("Export the charter"), f"{self.layer.name()}.charter.json", tr("Charter (*.charter.json)"))
        if path:
            export_charter(self.layer, path, self.charter, self.lists)
            self.win.say(tr("Saved: {p}").format(p=path))
        return path

    def import_(self):
        path = self._path(tr("Import a charter"), "", tr("Charter (*.charter.json *.json)"), save=False)
        if path:
            return self._apply_file(path, self.layer)
        return None

    def to_layer(self):
        from qgis.PyQt.QtWidgets import QInputDialog

        from .travel import export_charter

        others = [lyr for lyr in QgsProject.instance().mapLayers().values()
                  if isinstance(lyr, QgsVectorLayer) and lyr is not self.layer and not self.win.is_lookup(lyr)]
        if not others:
            self.win.say(tr("There is no other vector layer in the project."))
            return None
        if self.win.next_layer is not None:
            target = self.win.next_layer
        else:
            name, ok = QInputDialog.getItem(self, tr("Apply to another layer"), tr("Layer"),
                                            [lyr.name() for lyr in others], 0, False)
            if not ok:
                return None
            target = next(lyr for lyr in others if lyr.name() == name)
        self.win.save_all()
        import tempfile

        tmp = os.path.join(tempfile.mkdtemp(), "charter.json")
        export_charter(self.layer, tmp, self.charter, self.lists)
        return self._apply_file(tmp, target)

    def _apply_file(self, path, target):
        from . import travel

        try:
            charter, lists, _src = travel.read_charter_file(path)
        except (OSError, ValueError) as e:
            self.win.say(tr("This file is not a charter: {e}").format(e=e), error=True)
            return None
        mapping = travel.match_fields(charter, target)
        d = MappingDialog(charter, target, mapping, self)
        self.win.last_mapping_dialog = d
        if self.win.ask and not d.exec():
            return None
        mapping, add = d.result_mapping()
        merged, renamed = travel.merge_lists(lists, model.project_lists())
        c = travel.remap(charter, target, mapping, add, renamed)
        from .apply import apply_charter

        try:
            apply_charter(target, c, merged, section="travel")
        except RuntimeError as e:
            self.win.say(str(e), error=True)
            return None
        self.win.say(tr("The charter was applied to {l}.").format(l=target.name()))
        if target is self.layer:
            self.win.load_layer(target)
        return target

    def use_phone_form(self):
        from .travel import phone_form

        self.win.save_all()
        form = phone_form(self.charter, self.lists)
        QgsSettings().setValue("biruni/field-photos/form", json.dumps(form, ensure_ascii=False))
        self.win.say(tr("The Field Photos form now has {n} questions from this charter.").format(n=len(form)))
        return form

    def save_phone_form(self):
        from .travel import phone_form

        self.win.save_all()
        form = phone_form(self.charter, self.lists)
        path = self._path(tr("Save the phone form"), f"{self.layer.name()}_form.json", tr("Form (*.json)"))
        if path:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(form, f, ensure_ascii=False, indent=1)
            self.win.say(tr("Saved: {p}").format(p=path))
        return path


class MappingDialog(QDialog):
    """Which field of the layer takes each field of the charter."""

    def __init__(self, charter, layer, mapping, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Match the fields") + f" - {layer.name()}")
        self.resize(560, 460)
        v = QVBoxLayout(self)
        v.addWidget(_hint(tr("Each field of the charter goes to the field of {l} chosen here. A field with no match "
                             "can be added to the layer.").format(l=layer.name())))
        self.table = _table([tr("Charter field"), tr("Layer field")], stretch=(1,))
        names = [f.name() for f in layer.fields()]
        self.combos = []
        for spec in charter["fields"]:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, _item(f"{spec['name']}  ·  {model.field_label(spec)}", READONLY))
            cb = QComboBox()
            cb.addItem(tr("(none)"), "")
            for n in names:
                cb.addItem(n, n)
            cb.setCurrentIndex(max(0, cb.findData(mapping.get(spec["name"]) or "")))
            self.table.setCellWidget(r, 1, cb)
            self.combos.append((spec["name"], cb))
        v.addWidget(self.table, 1)
        self.add = QCheckBox(tr("Add the fields that have no match"))
        self.add.setChecked(True)
        v.addWidget(self.add)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def result_mapping(self):
        return {name: cb.currentData() or "" for name, cb in self.combos}, self.add.isChecked()


# ---- Change log ----------------------------------------------------------------------------------------------------
class LogPage(Page):
    key = "log"

    def __init__(self, win):
        super().__init__(win, tr("Change log"), tr("Who changed the charter, when, and what - kept in the project."))
        self.only = QCheckBox(tr("This layer only"))
        self.only.setChecked(True)
        self.only.toggled.connect(self.load)
        self.v.addWidget(self.only)
        self.table = _table([tr("When"), tr("Who"), tr("Layer"), tr("Section"), tr("What")], stretch=(4,))
        self.v.addWidget(self.table, 1)

    def load(self, *_):
        names = {k: label for k, label, _i in section_names()}
        names["travel"] = tr("Dictionary and sharing")
        self.table.setRowCount(0)
        for e in reversed(model.read_log()):
            if self.only.isChecked() and self.layer is not None and e.get("layer") != self.layer.name():
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, v in enumerate((e.get("time", "").replace("T", "  "), e.get("user"), e.get("layer"),
                                   names.get(e.get("section"), e.get("section")), e.get("what"))):
                self.table.setItem(r, c, _item(v, READONLY))


# ---- the window ----------------------------------------------------------------------------------------------------
class CharterWindow(QDialog):
    def __init__(self, plugin, layer=None, section="fields", parent=None):
        mw = plugin.iface.mainWindow() if plugin is not None else None
        super().__init__(parent or mw)
        self.plugin = plugin
        self.setObjectName("BiruniCharter")
        self.setWindowTitle(tr("Data Charter"))
        self.resize(1280, 760)
        self.ask = True                 # False in tests: no questions, no files opened
        self.next_path = []             # tests: file names instead of file dialogs
        self.next_layer = None
        self.last_mapping_dialog = None
        self.layer = None
        self.charter = model.new_charter()
        self.lists = {}
        self.deleted = []
        self.watcher = None
        self._alter = (True, "")
        self._build()
        self.refresh_theme()
        self.load_layer(layer)
        self.show_section(section or "fields")

    # -- build --------------------------------------------------------------------------------------------------
    def _build(self):
        from qgis.gui import QgsMapLayerComboBox

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 10)
        v.setSpacing(10)
        top = QHBoxLayout()
        title = QLabel(tr("Data Charter"))
        title.setObjectName("PageTitle")
        top.addWidget(title)
        top.addSpacing(16)
        top.addWidget(QLabel(tr("Layer")))
        self.layer_box = QgsMapLayerComboBox()
        self.layer_box.setFilters(Qgis.LayerFilter.VectorLayer)
        self.layer_box.setMinimumWidth(260)
        top.addWidget(self.layer_box)
        top.addStretch(1)
        self.badge = QPushButton(tr("Health"))
        self.badge.setObjectName("Badge")
        self.badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self.badge.clicked.connect(lambda: self.show_section("health"))
        top.addWidget(self.badge)
        v.addLayout(top)
        body = QHBoxLayout()
        self.sections = QListWidget()
        self.sections.setObjectName("Sections")
        self.sections.setIconSize(QSize(20, 20))
        self.sections.setFixedWidth(220)
        self.stack = QStackedWidget()
        self.pages = {}
        for cls in (FieldsPage, ListsPage, KindsPage, RulesPage, LinksPage, LearnPage, HealthPage, SharePage, LogPage):
            page = cls(self)
            self.pages[cls.key] = page
            self.stack.addWidget(page)
        for key, label, _icon in section_names():
            it = QListWidgetItem(label)
            it.setData(ROLE, key)
            self.sections.addItem(it)
        self.sections.currentRowChanged.connect(self._section_changed)
        body.addWidget(self.sections)
        body.addWidget(self.stack, 1)
        v.addLayout(body, 1)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("Line")
        v.addWidget(line)
        bottom = QHBoxLayout()
        self.status = QLabel()
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)
        self.apply_btn = QPushButton(tr("Apply"))
        self.apply_btn.setObjectName("Primary")
        self.apply_btn.clicked.connect(lambda: self.apply())
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.close)
        bottom.addWidget(self.apply_btn)
        bottom.addWidget(close)
        v.addLayout(bottom)
        self.layer_box.layerChanged.connect(self._layer_picked)

    def refresh_theme(self):
        from .. import icons

        t = self._tokens()
        self.setStyleSheet(charter_qss(t))
        for i, (_key, _label, icon) in enumerate(section_names()):
            if icons.exists(icon):
                self.sections.item(i).setIcon(icons.icon(icon, t["icon"]))
        if icons.exists("heart-rate-monitor"):
            self.badge.setIcon(icons.icon("heart-rate-monitor", t["icon"]))
        self._show_badge()

    def _tokens(self):
        theme = getattr(self.plugin, "theme", None)
        if theme is not None:
            try:
                return theme.tokens()
            except Exception:  # noqa: BLE001
                pass
        from ..theme import TOKENS

        return TOKENS["light"]

    # -- layer ---------------------------------------------------------------------------------------------------
    @staticmethod
    def is_lookup(layer):
        from .apply import CHOICES_PROP

        return isinstance(layer, QgsVectorLayer) and bool(layer.customProperty(CHOICES_PROP, False))

    def is_key(self, name):
        if self.layer is None or not name:
            return False
        idx = self.layer.fields().indexOf(name)
        return idx >= 0 and (idx in self.layer.primaryKeyAttributes() or name.lower() in ("fid", "ogc_fid"))

    def alter_state(self):
        return self._alter

    def _layer_picked(self, layer):
        if layer is not self.layer:
            self.load_layer(layer)

    def load_layer(self, layer):
        from .apply import can_alter

        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        if not isinstance(layer, QgsVectorLayer) or self.is_lookup(layer):
            layer = None
        self.layer = layer
        lookups = [lyr for lyr in QgsProject.instance().mapLayers().values() if self.is_lookup(lyr)]
        self.layer_box.setExceptedLayerList(lookups)
        self.layer_box.blockSignals(True)
        self.layer_box.setLayer(layer)
        self.layer_box.blockSignals(False)
        self.lists = model.project_lists()
        self.deleted = []
        self.say("")
        if layer is None:
            self.charter = model.new_charter()
            self._alter = (False, tr("Select a vector layer."))
        else:
            self.charter, seeded = model.read_charter(layer, self.lists)
            self.lists.update(seeded)
            self._alter = can_alter(layer)
            from .health import HealthWatcher

            self.watcher = HealthWatcher(layer, parent=self)
            self.watcher.updated.connect(self._health_updated)
        for page in self.pages.values():
            page.load()
        self.apply_btn.setEnabled(layer is not None)
        self.refresh_health()

    # -- sections ------------------------------------------------------------------------------------------------
    def show_section(self, key):
        keys = [k for k, _l, _i in section_names()]
        if key in keys:
            self.sections.setCurrentRow(keys.index(key))

    def _section_changed(self, row):
        cur = self.stack.currentWidget()
        if isinstance(cur, Page):
            cur.save()
        page = self.stack.widget(row)
        if page is not None:
            page.load()
            self.stack.setCurrentWidget(page)

    def save_all(self):
        cur = self.stack.currentWidget()
        if isinstance(cur, Page):
            cur.save()

    def say(self, text, error=False):
        self.status.setText(text)
        self.status.setStyleSheet("color: #DC2626;" if error else "")

    # -- apply -----------------------------------------------------------------------------------------------------
    def apply(self, section=None, quiet=False):
        from .apply import apply_charter

        if self.layer is None:
            return False
        self.save_all()
        section = section or self.pages_key()
        try:
            stored, text = apply_charter(self.layer, self.charter, self.lists, self.deleted, section=section)
        except RuntimeError as e:
            self.say(str(e), error=True)
            return False
        self.deleted = []
        self.charter = model.read_charter(self.layer, self.lists)[0]
        self.lists = model.project_lists()
        cur = self.stack.currentWidget()
        if isinstance(cur, Page):
            cur.load()
        self.refresh_health()
        if not quiet:
            msg = tr("The charter was applied to {l}.").format(l=self.layer.name())
            self.say(msg + (f" ({text})" if text else ""))
        return True

    def pages_key(self):
        cur = self.stack.currentWidget()
        return getattr(cur, "key", "")

    def rebuild_layer(self):
        from .apply import can_alter, rebuild_into_gpkg

        if self.layer is None:
            return
        if self.ask and QMessageBox.question(
                self, APP_NAME, tr("Copy {l} into a GeoPackage and use the copy from now on? The original file is "
                                   "not changed.").format(l=self.layer.name())) != QMessageBox.StandardButton.Yes:
            return
        try:
            path = rebuild_into_gpkg(self.layer)
        except RuntimeError as e:
            self.say(str(e), error=True)
            return
        self._alter = can_alter(self.layer)
        self.pages["fields"].load()
        self.say(tr("The layer now reads from {p}.").format(p=path))

    # -- health ----------------------------------------------------------------------------------------------------
    def refresh_health(self):
        rep = self.watcher.refresh() if self.watcher is not None else None
        if rep is None:
            self._health_updated(None)
        return rep

    def _health_updated(self, rep):
        self.report = rep
        self.pages["health"].show_report(rep)
        self._show_badge()

    def _show_badge(self):
        rep = getattr(self, "report", None)
        if rep is None:
            self.badge.setText(tr("Health"))
            self.badge.setStyleSheet("")
            return
        self.badge.setText(tr("Health {s}%").format(s=rep.score))
        self.badge.setStyleSheet(f"color: {score_colour(rep.score)};")
        self.badge.setToolTip(tr("{n} problems").format(n=sum(it.count for it in rep.items)))

    def closeEvent(self, e):  # noqa: N802
        if self.watcher is not None:
            self.watcher.stop()
            self.watcher = None
        _WINDOWS.pop(id(self.plugin), None)
        super().closeEvent(e)


_WINDOWS = {}


def open_charter(plugin, section=None, layer=None):
    """Opens (or brings back) the charter window on the active layer. section: fields, lists, kinds, rules, links,
    learn, health, share or log."""
    layer = layer or plugin.iface.activeLayer()
    w = _WINDOWS.get(id(plugin))
    if w is None:
        w = CharterWindow(plugin, layer, section or "fields")
        w.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        _WINDOWS[id(plugin)] = w
        theme = getattr(plugin, "theme", None)
        if theme is not None and hasattr(theme, "on_change"):
            theme.on_change(lambda: _WINDOWS.get(id(plugin)) is not None and _WINDOWS[id(plugin)].refresh_theme())
    else:
        if isinstance(layer, QgsVectorLayer) and layer is not w.layer:
            w.load_layer(layer)
        if section:
            w.show_section(section)
    w.show()
    w.raise_()
    w.activateWindow()
    return w


def layer_menu_action(plugin):
    """A QAction for the Layers panel's context menu (vector layers): opens the charter of the clicked layer."""
    from qgis.PyQt.QtGui import QAction

    from .. import icons

    act = QAction(tr("Data Charter…"), plugin.iface.mainWindow())
    if icons.exists("license"):
        act.setIcon(icons.icon("license", "#0E7C74"))
    act.triggered.connect(lambda: open_charter(plugin, None, plugin.iface.activeLayer()))
    return act
