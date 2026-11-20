"""The capture schema editor: design and customize job checklist trees on the PC."""
import copy
import json
import os
import re

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                                 QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
                                 QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..i18n import tr
from .capture import MAX_DEPTH, MAX_NODES, MAX_PHOTOS, MAX_REPEAT, NODE_ID_RE, clean_questions, clean_schema
from .capture_templates import builtin_templates
from .form_dialog import FormDialog


def library_folder(settings_dir=None):
    base = settings_dir or QgsApplication.qgisSettingsDirPath()
    return os.path.join(base, "biruni", "capture_schemas")


def load_office_schemas(settings_dir=None):
    """Return all schemas saved in the office library, or built-in templates merged with office overrides."""
    by_id = {t["id"]: t for t in builtin_templates()}
    folder = library_folder(settings_dir)
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    s = clean_schema(json.load(f))
                by_id[s["id"]] = s
            except (OSError, ValueError):
                continue
    return list(by_id.values())


def save_office_schema(schema, settings_dir=None):
    """Validate and save a schema into `<qgisSettingsDir>/biruni/capture_schemas/<id>.json`."""
    s = clean_schema(schema)
    folder = library_folder(settings_dir)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{s['id']}.json")
    if os.path.isfile(path):
        try:
            old = clean_schema(json.load(open(path, encoding="utf-8")))
            if old != s and s["rev"] <= old["rev"]:
                s = dict(s, rev=old["rev"] + 1)
        except (OSError, ValueError):
            pass
    part = path + ".part"
    with open(part, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, path)
    return s


def _slug_id(label, taken):
    s = re.sub(r"[^a-z0-9_]+", "_", str(label or "").strip().lower()).strip("_")
    if not s or not s[0].isalpha():
        s = "item_" + s if s else "item"
    s = s[:20]
    cand, i = s, 2
    while cand in taken or not NODE_ID_RE.match(cand):
        cand = f"{s[:18]}_{i}"
        i += 1
    return cand


class SchemaDialog(QDialog):
    """Editor for a capture schema tree, node properties, and job/node questions."""

    def __init__(self, schema=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Capture template"))
        self.resize(880, 560)
        self._updating = False
        self._cur_item = None

        templates = load_office_schemas()
        init_schema = clean_schema(schema) if schema else copy.deepcopy(templates[0])
        self.schema = copy.deepcopy(init_schema)
        self._job_questions = list(self.schema.get("job") or ())

        v = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel(tr("Template:")))
        self.tpl_combo = QComboBox()
        for t in templates:
            self.tpl_combo.addItem(f"{t['title']} ({t['id']})", t)
        idx = next((i for i, t in enumerate(templates) if t["id"] == self.schema["id"]), 0)
        self.tpl_combo.setCurrentIndex(idx)
        self.tpl_combo.currentIndexChanged.connect(self._on_template_picked)
        top.addWidget(self.tpl_combo, 1)

        top.addWidget(QLabel(tr("ID:")))
        self.id_edit = QLineEdit(self.schema["id"])
        self.id_edit.setMaximumWidth(130)
        top.addWidget(self.id_edit)

        top.addWidget(QLabel(tr("Title:")))
        self.title_edit = QLineEdit(self.schema["title"])
        top.addWidget(self.title_edit, 1)

        self.job_q_btn = QPushButton()
        self.job_q_btn.clicked.connect(self._edit_job_questions)
        top.addWidget(self.job_q_btn)
        v.addLayout(top)

        mid = QHBoxLayout()
        left = QVBoxLayout()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Checklist item"), tr("Repeat"), tr("Photos"), tr("Questions")])
        self.tree.currentItemChanged.connect(self._on_item_selected)
        left.addWidget(self.tree, 1)

        tbtns = QHBoxLayout()
        for text, fn in (
            (tr("Add item"), lambda: self._add_node(child=False)),
            (tr("Add sub-item"), lambda: self._add_node(child=True)),
            (tr("Remove"), self._remove_node),
            (tr("Up"), lambda: self._move_node(-1)),
            (tr("Down"), lambda: self._move_node(1)),
            (tr("Import…"), self._import),
            (tr("Export…"), self._export),
        ):
            b = QPushButton(text)
            b.clicked.connect(fn)
            tbtns.addWidget(b)
        tbtns.addStretch(1)
        left.addLayout(tbtns)
        mid.addLayout(left, 3)

        self.panel = QFrame()
        form = QFormLayout(self.panel)
        self.node_id_edit = QLineEdit()
        self.node_id_edit.textChanged.connect(self._apply_panel_to_item)
        form.addRow(tr("Item ID:"), self.node_id_edit)

        self.node_label_edit = QLineEdit()
        self.node_label_edit.textChanged.connect(self._apply_panel_to_item)
        form.addRow(tr("Label:"), self.node_label_edit)

        self.node_req_box = QCheckBox(tr("Required"))
        self.node_req_box.toggled.connect(self._apply_panel_to_item)
        form.addRow("", self.node_req_box)

        self.repeat_box = QCheckBox(tr("Repeating item (surveyor adds in the field)"))
        self.repeat_box.toggled.connect(self._on_repeat_toggled)
        form.addRow("", self.repeat_box)

        rrow = QHBoxLayout()
        self.rep_min = QSpinBox()
        self.rep_min.setRange(0, MAX_REPEAT)
        self.rep_min.valueChanged.connect(self._apply_panel_to_item)
        self.rep_max = QSpinBox()
        self.rep_max.setRange(1, MAX_REPEAT)
        self.rep_max.valueChanged.connect(self._apply_panel_to_item)
        rrow.addWidget(QLabel(tr("Min:")))
        rrow.addWidget(self.rep_min)
        rrow.addWidget(QLabel(tr("Max:")))
        rrow.addWidget(self.rep_max)
        form.addRow(tr("Repeat count:"), rrow)

        self.photos_box = QCheckBox(tr("Requires photos"))
        self.photos_box.toggled.connect(self._on_photos_toggled)
        form.addRow("", self.photos_box)

        prow = QHBoxLayout()
        self.ph_min = QSpinBox()
        self.ph_min.setRange(0, MAX_PHOTOS)
        self.ph_min.valueChanged.connect(self._apply_panel_to_item)
        self.ph_max = QSpinBox()
        self.ph_max.setRange(0, MAX_PHOTOS)
        self.ph_max.valueChanged.connect(self._apply_panel_to_item)
        prow.addWidget(QLabel(tr("Min:")))
        prow.addWidget(self.ph_min)
        prow.addWidget(QLabel(tr("Max:")))
        prow.addWidget(self.ph_max)
        form.addRow(tr("Photos count:"), prow)

        self.node_q_btn = QPushButton()
        self.node_q_btn.clicked.connect(self._edit_node_questions)
        form.addRow(tr("Questions:"), self.node_q_btn)

        mid.addWidget(self.panel, 2)
        v.addLayout(mid, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

        self._load_schema(self.schema)

    def _update_job_q_btn(self):
        self.job_q_btn.setText(tr("Job questions ({n})…").format(n=len(self._job_questions)))

    def _load_schema(self, s):
        self._updating = True
        self.id_edit.setText(s.get("id", "custom"))
        self.title_edit.setText(s.get("title", ""))
        self._job_questions = list(s.get("job") or ())
        self._update_job_q_btn()
        self.tree.clear()
        for n in s.get("nodes") or ():
            self.tree.addTopLevelItem(self._make_tree_item(n))
        self.tree.expandAll()
        self._updating = False
        if self.tree.topLevelItemCount() > 0:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self._set_panel_item(None)

    def _make_tree_item(self, node):
        d = copy.deepcopy(node)
        d.pop("nodes", None)
        it = QTreeWidgetItem()
        it.setData(0, Qt.ItemDataRole.UserRole, d)
        self._refresh_item_text(it)
        for ch in node.get("nodes") or ():
            it.addChild(self._make_tree_item(ch))
        return it

    def _refresh_item_text(self, it):
        d = it.data(0, Qt.ItemDataRole.UserRole) or {}
        lbl = d.get("label") or d.get("id") or ""
        if not d.get("req", True):
            lbl += f" ({tr('optional')})"
        rep = d.get("repeat")
        rep_txt = f"⟲ {rep['min']}..{rep['max']}" if isinstance(rep, dict) else "—"
        ph = d.get("photos")
        ph_txt = f"{ph[0]}..{ph[1]}" if isinstance(ph, (list, tuple)) and len(ph) == 2 else "—"
        q_txt = str(len(d.get("q") or ())) if d.get("q") else "—"
        it.setText(0, lbl)
        it.setText(1, rep_txt)
        it.setText(2, ph_txt)
        it.setText(3, q_txt)

    def _on_item_selected(self, cur, _prev):
        self._set_panel_item(cur)

    def _set_panel_item(self, it):
        self._updating = True
        self._cur_item = it
        self.panel.setEnabled(it is not None)
        if it is None:
            self.node_id_edit.clear()
            self.node_label_edit.clear()
            self.node_q_btn.setText(tr("Questions…"))
            self._updating = False
            return
        d = it.data(0, Qt.ItemDataRole.UserRole) or {}
        self.node_id_edit.setText(d.get("id", ""))
        self.node_label_edit.setText(d.get("label", ""))
        self.node_req_box.setChecked(bool(d.get("req", True)))
        rep = d.get("repeat")
        has_rep = isinstance(rep, dict)
        self.repeat_box.setChecked(has_rep)
        self.rep_min.setEnabled(has_rep)
        self.rep_max.setEnabled(has_rep)
        self.rep_min.setValue(rep.get("min", 1) if has_rep else 1)
        self.rep_max.setValue(rep.get("max", 10) if has_rep else 10)

        ph = d.get("photos")
        has_ph = isinstance(ph, (list, tuple)) and len(ph) == 2
        self.photos_box.setChecked(has_ph)
        self.ph_min.setEnabled(has_ph)
        self.ph_max.setEnabled(has_ph)
        self.ph_min.setValue(ph[0] if has_ph else 1)
        self.ph_max.setValue(ph[1] if has_ph else 3)

        n_q = len(d.get("q") or ())
        self.node_q_btn.setText(tr("Questions ({n})…").format(n=n_q) if n_q else tr("Questions…"))
        self._updating = False

    def _on_repeat_toggled(self, on):
        self.rep_min.setEnabled(on)
        self.rep_max.setEnabled(on)
        self._apply_panel_to_item()

    def _on_photos_toggled(self, on):
        self.ph_min.setEnabled(on)
        self.ph_max.setEnabled(on)
        self._apply_panel_to_item()

    def _apply_panel_to_item(self, *_a):
        if self._updating or self._cur_item is None:
            return
        d = dict(self._cur_item.data(0, Qt.ItemDataRole.UserRole) or {})
        d["id"] = self.node_id_edit.text().strip()
        d["label"] = self.node_label_edit.text().strip()
        d["req"] = self.node_req_box.isChecked()
        if self.repeat_box.isChecked():
            d["repeat"] = {"min": self.rep_min.value(), "max": self.rep_max.value()}
        else:
            d.pop("repeat", None)
        if self.photos_box.isChecked():
            d["photos"] = [self.ph_min.value(), self.ph_max.value()]
        else:
            d.pop("photos", None)
        self._cur_item.setData(0, Qt.ItemDataRole.UserRole, d)
        self._refresh_item_text(self._cur_item)

    def _all_node_ids(self):
        out = set()

        def _walk(it):
            d = it.data(0, Qt.ItemDataRole.UserRole) or {}
            if d.get("id"):
                out.add(d["id"])
            for i in range(it.childCount()):
                _walk(it.child(i))

        for i in range(self.tree.topLevelItemCount()):
            _walk(self.tree.topLevelItem(i))
        return out

    def _item_depth(self, it):
        d = 1
        p = it.parent() if it is not None else None
        while p is not None:
            d += 1
            p = p.parent()
        return d

    def _add_node(self, child=False):
        taken = self._all_node_ids()
        if len(taken) >= MAX_NODES:
            QMessageBox.warning(self, self.windowTitle(), tr("A template has at most {n} items.").format(n=MAX_NODES))
            return
        cur = self.tree.currentItem()
        parent = (cur if child else (cur.parent() if cur is not None else None)) if cur is not None else None
        if parent is not None and self._item_depth(parent) >= MAX_DEPTH:
            QMessageBox.warning(self, self.windowTitle(), tr("Maximum nesting depth ({n}) reached.").format(n=MAX_DEPTH))
            return
        nid = _slug_id("item", taken)
        node = {"id": nid, "label": tr("New item"), "req": True, "photos": [1, 3]}
        it = self._make_tree_item(node)
        if parent is None:
            self.tree.addTopLevelItem(it)
        else:
            parent.addChild(it)
            parent.setExpanded(True)
        self.tree.setCurrentItem(it)
        self.node_label_edit.setFocus()
        self.node_label_edit.selectAll()

    def _remove_node(self):
        cur = self.tree.currentItem()
        if cur is None:
            return
        p = cur.parent()
        if p is None:
            idx = self.tree.indexOfTopLevelItem(cur)
            self.tree.takeTopLevelItem(idx)
        else:
            p.removeChild(cur)

    def _move_node(self, step):
        cur = self.tree.currentItem()
        if cur is None:
            return
        p = cur.parent()
        if p is None:
            idx = self.tree.indexOfTopLevelItem(cur)
            nxt = idx + step
            if 0 <= nxt < self.tree.topLevelItemCount():
                self.tree.takeTopLevelItem(idx)
                self.tree.insertTopLevelItem(nxt, cur)
                self.tree.setCurrentItem(cur)
        else:
            idx = p.indexOfChild(cur)
            nxt = idx + step
            if 0 <= nxt < p.childCount():
                p.takeChild(idx)
                p.insertChild(nxt, cur)
                self.tree.setCurrentItem(cur)

    def _edit_job_questions(self):
        dlg = FormDialog(self._job_questions, self, keep=False, title=True)
        dlg.setWindowTitle(tr("Job questions"))
        if dlg.exec():
            self._job_questions = clean_questions(dlg.form, keep=False, title=True)
            self._update_job_q_btn()

    def _edit_node_questions(self):
        if self._cur_item is None:
            return
        d = dict(self._cur_item.data(0, Qt.ItemDataRole.UserRole) or {})
        dlg = FormDialog(d.get("q") or [], self, keep=False, title=True)
        dlg.setWindowTitle(tr("Questions for {item}").format(item=d.get("label") or d.get("id") or ""))
        if dlg.exec():
            qs = clean_questions(dlg.form, keep=False, title=True)
            if qs:
                d["q"] = qs
            else:
                d.pop("q", None)
            self._cur_item.setData(0, Qt.ItemDataRole.UserRole, d)
            self._refresh_item_text(self._cur_item)
            self._set_panel_item(self._cur_item)

    def _on_template_picked(self, idx):
        if self._updating or idx < 0:
            return
        t = self.tpl_combo.itemData(idx)
        if isinstance(t, dict):
            self.schema = copy.deepcopy(t)
            self._load_schema(self.schema)

    def _collect_item(self, it):
        d = copy.deepcopy(it.data(0, Qt.ItemDataRole.UserRole) or {})
        ch = [self._collect_item(it.child(i)) for i in range(it.childCount())]
        if ch:
            d["nodes"] = ch
        else:
            d.pop("nodes", None)
        return d

    def build_raw_schema(self):
        nodes = [self._collect_item(self.tree.topLevelItem(i)) for i in range(self.tree.topLevelItemCount())]
        return {
            "biruni_capture": 1,
            "id": self.id_edit.text().strip(),
            "rev": int(self.schema.get("rev", 1)),
            "title": self.title_edit.text().strip(),
            "job": list(self._job_questions),
            "nodes": nodes,
        }

    def _check(self):
        try:
            return None, clean_schema(self.build_raw_schema())
        except ValueError as e:
            return str(e), None

    def _export(self):
        err, s = self._check()
        if err:
            QMessageBox.warning(self, self.windowTitle(), err)
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Export capture template"), f"{s['id']}.json", "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=1)

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Import capture template"), "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                s = clean_schema(json.load(f))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, self.windowTitle(), tr("This file is not a valid capture template ({e}).").format(e=e))
            return
        self.schema = s
        self._load_schema(s)

    def _ok(self):
        err, s = self._check()
        if err:
            QMessageBox.warning(self, self.windowTitle(), err)
            return
        self.schema = save_office_schema(s)
        self.accept()
