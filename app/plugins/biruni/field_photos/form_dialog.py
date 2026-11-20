"""The form designer: the questions the phone asks with each photo (server.clean_form keeps the rules)."""
import json

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                                 QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
                                 QTableWidgetItem, QVBoxLayout)

from ..i18n import tr
from .server import MAX_FIELDS, clean_form

def type_names():
    return (("text", tr("Text")), ("number", tr("Number")), ("integer", tr("Whole number")),
            ("choice", tr("One choice")), ("multi", tr("Several choices")), ("yesno", tr("Yes / No")),
            ("date", tr("Date")))


def example_form():
    """A ready form to start from (a street light survey)."""
    return clean_form([
        {"label": tr("Pole number"), "type": "text", "req": True},
        {"label": tr("Condition"), "type": "choice", "req": True,
         "choices": [tr("Good"), tr("Needs repair"), tr("Broken")]},
        {"label": tr("Problems"), "type": "multi",
         "choices": [tr("Rust"), tr("Leaning"), tr("Lamp off"), tr("Open cover")]},
        {"label": tr("Height (m)"), "type": "number", "min": 0, "max": 40},
        {"label": tr("Lamps"), "type": "integer", "min": 1, "max": 8},
        {"label": tr("Safe to reach?"), "type": "yesno"},
        {"label": tr("Surveyor"), "type": "text", "keep": True},
    ])


def _num_text(v):
    return "" if v is None else (str(int(v)) if float(v).is_integer() else str(v))


class FormDialog(QDialog):
    def __init__(self, form, parent=None, keep=True, title=False):
        super().__init__(parent)
        self._has_keep = bool(keep)
        self._has_title = bool(title)
        self.setWindowTitle(tr("Form for the photos"))
        self.resize(820, 440)
        v = QVBoxLayout(self)
        intro = QLabel(tr("The phone asks these questions with each photo; the answers become columns of the photos "
                          "layer. Required questions must be answered before the camera opens, numbers are checked "
                          "against their limits, and \"Keep\" leaves the answer filled in for the next photo."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.table = QTableWidget(0, 8 if self._has_title else 7)
        headers = [tr("Question"), tr("Type"), tr("Choices (separated by commas)"),
                   tr("Min"), tr("Max"), tr("Required"), tr("Keep")]
        if self._has_title:
            headers.append(tr("Title"))
        self.table.setHorizontalHeaderLabels(headers)
        if not self._has_keep:
            self.table.setColumnHidden(6, True)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for c in (1, 3, 4, 5, 6) + ((7,) if self._has_title else ()):
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, fn in ((tr("Add question"), lambda: self._add({"type": "text"}, edit=True)),
                         (tr("Remove"), self._remove), (tr("Up"), lambda: self._move(-1)),
                         (tr("Down"), lambda: self._move(1)), (tr("Example"), self._example),
                         (tr("Import…"), self._import), (tr("Export…"), self._export)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self.form = self._clean(form)
        self._fill(self.form)

    def _clean(self, raw):
        raw_list = list(raw) if isinstance(raw, (list, tuple)) else []
        cleaned = clean_form(raw_list)
        if self._has_title:
            titles = {str(f.get("label", "")).strip().lower(): bool(f.get("title"))
                      for f in raw_list if isinstance(f, dict)}
            by_name = {str(f.get("name", "")): bool(f.get("title"))
                       for f in raw_list if isinstance(f, dict) and f.get("name")}
            for f in cleaned:
                if by_name.get(f["name"]) or titles.get(f["label"].lower()):
                    f["title"] = True
        if not self._has_keep:
            for f in cleaned:
                f["keep"] = False
        return cleaned

    # ---- rows ------------------------------------------------------------------
    def _fill(self, form):
        self.table.setRowCount(0)
        for f in form:
            self._add(f)

    def _add(self, f, edit=False):
        if self.table.rowCount() >= MAX_FIELDS:
            QMessageBox.information(self, self.windowTitle(), tr("A form has at most {n} questions.").format(n=MAX_FIELDS))
            return
        r = self.table.rowCount()
        self.table.insertRow(r)
        self._set_row(r, f)
        if edit:
            self.table.setCurrentCell(r, 0)
            self.table.editItem(self.table.item(r, 0))

    def _set_row(self, r, f):
        label = QTableWidgetItem(f.get("label", ""))
        label.setData(Qt.ItemDataRole.UserRole, f.get("name", ""))  # the column keeps its name when renamed
        self.table.setItem(r, 0, label)
        kind = QComboBox()
        for key, text in type_names():
            kind.addItem(text, key)
        kind.setCurrentIndex(max(0, kind.findData(f.get("type", "text"))))
        kind.currentIndexChanged.connect(lambda _i, w=kind: self._type_changed(w))
        self.table.setCellWidget(r, 1, kind)
        self.table.setItem(r, 2, QTableWidgetItem(", ".join(f.get("choices") or ())))
        self.table.setItem(r, 3, QTableWidgetItem(_num_text(f.get("min"))))
        self.table.setItem(r, 4, QTableWidgetItem(_num_text(f.get("max"))))
        for c, key in ((5, "req"), (6, "keep")) + (((7, "title"),) if self._has_title else ()):
            it = QTableWidgetItem()
            it.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            it.setCheckState(Qt.CheckState.Checked if f.get(key) else Qt.CheckState.Unchecked)
            self.table.setItem(r, c, it)
        self._enable(r)

    def _type_changed(self, widget):
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 1) is widget:
                self._enable(r)

    def _enable(self, r):
        t = self.table.cellWidget(r, 1).currentData()
        for c, on in ((2, t in ("choice", "multi")), (3, t in ("number", "integer")), (4, t in ("number", "integer"))):
            it = self.table.item(r, c)
            flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
            it.setFlags(flags | Qt.ItemFlag.ItemIsEditable if on else Qt.ItemFlag.NoItemFlags)

    def _row(self, r):
        def text(c):
            return (self.table.item(r, c).text() if self.table.item(r, c) else "").strip()

        def num(c):
            try:
                return float(text(c).replace(",", ".")) if text(c) else None
            except ValueError:
                return "bad"

        label = self.table.item(r, 0)
        d = {"name": label.data(Qt.ItemDataRole.UserRole) or "" if label else "", "label": text(0),
             "type": self.table.cellWidget(r, 1).currentData(),
             "choices": [c.strip() for c in text(2).replace("،", ",").split(",") if c.strip()],
             "min": num(3), "max": num(4),
             "req": self.table.item(r, 5).checkState() == Qt.CheckState.Checked,
             "keep": self._has_keep and self.table.item(r, 6).checkState() == Qt.CheckState.Checked}
        if self._has_title and self.table.item(r, 7) is not None:
            if self.table.item(r, 7).checkState() == Qt.CheckState.Checked:
                d["title"] = True
        return d

    def rows(self):
        return [self._row(r) for r in range(self.table.rowCount())]

    def _remove(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def _move(self, step):
        r = self.table.currentRow()
        rows = self.rows()
        if r < 0 or not 0 <= r + step < len(rows):
            return
        rows[r], rows[r + step] = rows[r + step], rows[r]
        self._fill([dict(x, min=None if x["min"] == "bad" else x["min"],
                         max=None if x["max"] == "bad" else x["max"]) for x in rows])
        self.table.setCurrentCell(r + step, 0)

    def _example(self):
        if self.table.rowCount() and QMessageBox.question(
                self, self.windowTitle(), tr("Replace the questions with the example?")) != QMessageBox.StandardButton.Yes:
            return
        self._fill(example_form())

    # ---- share a form between computers ----------------------------------------------
    def _export(self):
        problem, form = self._check()
        if problem:
            QMessageBox.warning(self, self.windowTitle(), problem)
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Export the form"), "form.json", "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"biruni_form": 1, "questions": form}, f, ensure_ascii=False, indent=1)

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Import a form"), "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            form = self._clean(data.get("questions") if isinstance(data, dict) else data)
        except (OSError, ValueError, AttributeError) as e:
            QMessageBox.warning(self, self.windowTitle(), tr("This file is not a form ({e}).").format(e=e))
            return
        if not form:
            QMessageBox.warning(self, self.windowTitle(), tr("This file has no questions."))
            return
        self._fill(form)

    # ---- the rules ----------------------------------------------------------------
    def _check(self):
        """(problem, form): the first thing to fix, or None and the form as it will be kept."""
        rows, seen = self.rows(), set()
        for i, f in enumerate(rows, 1):
            if not f["label"]:
                if f["choices"] or f["req"]:
                    return tr("Question {i} has no text.").format(i=i), None
                continue
            if f["label"].lower() in seen:
                return tr("Two questions are called \"{q}\".").format(q=f["label"]), None
            seen.add(f["label"].lower())
            if f["type"] in ("choice", "multi") and len(set(f["choices"])) < 2:
                return tr("\"{q}\" needs at least two choices, separated by commas.").format(q=f["label"]), None
            if "bad" in (f["min"], f["max"]):
                return tr("The limits of \"{q}\" must be numbers.").format(q=f["label"]), None
            if f["min"] is not None and f["max"] is not None and f["min"] > f["max"]:
                return tr("In \"{q}\" the minimum is larger than the maximum.").format(q=f["label"]), None
        return None, self._clean([f for f in rows if f["label"]])

    def _ok(self):
        problem, form = self._check()
        if problem:
            QMessageBox.warning(self, self.windowTitle(), problem)
            return
        self.form = form
        self.accept()
