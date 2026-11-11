"""The room's merge rules and number ranges, edited by the room's admin (rank 0, hosting) and shown read-only to
everyone else (rules.py)."""
from qgis.PyQt.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
                                 QLabel, QMessageBox, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
                                 QVBoxLayout)

from ..i18n import tr
from . import rules


def _can_edit(node):
    return node is not None and node.hub is not None and node.my_rank() == 0


def _members(node):
    out = [{"id": node.me_id, "name": node.ident.get("name", "")}]
    src = node.hub.store.members() if node.hub is not None else node.roster
    out += [{"id": m["id"], "name": m.get("name", "")} for m in src if m.get("id") != node.me_id
            and m.get("state") != "out"]
    return out


def _layer_name(node, key):
    lyr = node.layers.get(key)
    return lyr.name() if lyr is not None else key


class RulesDialog(QDialog):
    """Per layer: field -> rule."""

    def __init__(self, node, parent=None):
        super().__init__(parent)
        self.node = node
        self.edit = _can_edit(node)
        self.setWindowTitle(tr("Merge Rules"))
        lay = QVBoxLayout(self)
        note = QLabel(tr("What happens when two people change the same field of the same feature at the same time. "
                         "The host decides by these rules, so everyone ends with the same values.")
                      if self.edit else
                      tr("Only the room's admin can change these rules, while hosting the room."))
        note.setWordWrap(True)
        lay.addWidget(note)
        self.layer = QComboBox()
        for key in sorted(node.layers):
            self.layer.addItem(_layer_name(node, key), key)
        lay.addWidget(self.layer)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([tr("Field"), tr("When both changed it")])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table)
        c = rules.constitution(node.store, node.lookup_member)
        self.rules = {k: dict(v) for k, v in (c.get("layers") or {}).items()}
        self.version = int(c.get("v", 0))
        self.layer.currentIndexChanged.connect(self._fill)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
                              if self.edit else QDialogButtonBox.StandardButton.Close)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fill()
        self.resize(520, 460)

    def _fill(self):
        key = self.layer.currentData()
        lyr = self.node.layers.get(key)
        names = [f.name() for f in lyr.fields()] if lyr is not None else []
        cur = self.rules.get(key, {})
        self.table.setRowCount(len(names))
        for r, name in enumerate(names):
            self.table.setItem(r, 0, QTableWidgetItem(name))
            box = QComboBox()
            for rule in rules.RULES:
                box.addItem(tr(rules.RULE_LABELS[rule]), rule)
            box.setCurrentIndex(rules.RULES.index(cur.get(name, "keep")))
            box.setEnabled(self.edit)
            box.currentIndexChanged.connect(lambda _i, n=name, b=box: self._set(n, b.currentData()))
            self.table.setCellWidget(r, 1, box)

    def _set(self, field, rule):
        d = self.rules.setdefault(self.layer.currentData(), {})
        if rule == "keep":
            d.pop(field, None)
        else:
            d[field] = rule

    def save(self):
        if not self.edit:
            return
        signed = rules.make_constitution(self.node.ident["seed"], self.node.me_id, self.rules, self.version + 1)
        self.node.store.put(rules.CKEY, signed)
        self.node.hub._settings = (0.0, {})
        self.node.hub._bump()
        self.accept()


class NumbersDialog(QDialog):
    """Number ranges granted to members for a numbering field."""

    def __init__(self, node, parent=None):
        super().__init__(parent)
        self.node = node
        self.edit = _can_edit(node)
        self.setWindowTitle(tr("Number Ranges"))
        lay = QVBoxLayout(self)
        note = QLabel(tr("Each member numbers new features from their own range, even offline, so two people never "
                         "give the same number. A number outside its author's range, or used twice, is flagged.")
                      if self.edit else tr("Only the room's admin can grant numbers, while hosting the room."))
        note.setWordWrap(True)
        lay.addWidget(note)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([tr("Layer"), tr("Field"), tr("Member"), tr("From"), tr("To")])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        lay.addWidget(self.table)
        if self.edit:
            row = QHBoxLayout()
            self.layer, self.field, self.member = QComboBox(), QComboBox(), QComboBox()
            for key in sorted(node.layers):
                self.layer.addItem(_layer_name(node, key), key)
            self.layer.currentIndexChanged.connect(self._fields)
            for m in _members(node):
                self.member.addItem(m["name"] or m["id"][:8], m["id"])
            self.lo, self.hi = QSpinBox(), QSpinBox()
            for s in (self.lo, self.hi):
                s.setRange(0, 2_000_000_000)
            self.hi.setValue(99)
            add = QPushButton(tr("Grant"))
            add.clicked.connect(self.grant)
            rm = QPushButton(tr("Remove"))
            rm.clicked.connect(self.remove)
            for w in (self.layer, self.field, self.member, self.lo, self.hi, add, rm):
                row.addWidget(w)
            lay.addLayout(row)
            self._fields()
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fill()
        self.resize(640, 420)

    def _fields(self):
        self.field.clear()
        lyr = self.node.layers.get(self.layer.currentData())
        for f in (lyr.fields() if lyr is not None else []):
            if f.isNumeric():
                self.field.addItem(f.name())

    def _names(self):
        return {m["id"]: m["name"] for m in _members(self.node)}

    def _fill(self):
        names = self._names()
        docs = rules.grants(self.node.store, self.node.lookup_member)
        self.table.setRowCount(len(docs))
        for r, d in enumerate(docs):
            for c, text in enumerate((_layer_name(self.node, d["layer"]), d["field"],
                                      names.get(d["member"], d["member"][:8]), str(d["lo"]), str(d["hi"]))):
                self.table.setItem(r, c, QTableWidgetItem(text))
        self._docs = docs

    def _save(self, signed_list):
        self.node.store.put(rules.GKEY, signed_list)
        self.node.hub._settings = (0.0, {})
        self.node.hub._bump()
        self._fill()

    def grant(self):
        key, field, member = self.layer.currentData(), self.field.currentText(), self.member.currentData()
        lo, hi = self.lo.value(), self.hi.value()
        if not field or hi < lo:
            return
        for d in rules.grants(self.node.store, self.node.lookup_member, layer=key, field=field):
            if not (hi < d["lo"] or lo > d["hi"]):
                QMessageBox.warning(self, tr("Number Ranges"),
                                    tr("This range overlaps {0}-{1}, already granted.").format(d["lo"], d["hi"]))
                return
        cur = list(self.node.store.get(rules.GKEY) or [])
        cur.append(rules.make_grant(self.node.ident["seed"], self.node.me_id, key, field, member, lo, hi))
        self._save(cur)

    def remove(self):
        r = self.table.currentRow()
        if r < 0 or r >= len(self._docs):
            return
        gone = self._docs[r]
        self._save([g for g in (self.node.store.get(rules.GKEY) or []) if g.get("doc") != gone])


def open_rules(node, parent=None):
    if node is not None:
        RulesDialog(node, parent).exec()


def open_numbers(node, parent=None):
    if node is not None:
        NumbersDialog(node, parent).exec()
