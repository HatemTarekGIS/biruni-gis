"""Office progress view for a capture job: checklist tree, missing filter, unbound photos, duplicate instances."""
import datetime as dt
import os

from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
                                 QHeaderView, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from ..i18n import tr
from .capture import clean_cap, duplicate_instances, missing, progress_counts, tree
from .capture_jobs import export_job_report_dialog, resolve_job_schema


def _iso_to_seconds(val):
    if not val:
        return None
    if isinstance(val, (int, float)):
        return float(val) / 1000.0 if val > 1e11 else float(val)
    try:
        return dt.datetime.fromisoformat(str(val)).timestamp()
    except ValueError:
        return None


def unbound_job_records(job, job_id, merged_state, records):
    """Return photo records without `cap` that arrived during this job (e.g. from an older cached phone page)."""
    bound_ts = []
    unbound_cands = []
    for r in records or ():
        if not isinstance(r, dict) or not r.get("id"):
            continue
        raw_cap = r.get("cap")
        cap = clean_cap(raw_cap)
        if cap is not None:
            if cap["j"] == job_id:
                ts = _iso_to_seconds(r.get("taken") or r.get("t"))
                if ts is not None:
                    bound_ts.append(ts)
            continue
        if raw_cap is not None:
            continue
        unbound_cands.append(r)

    if not unbound_cands:
        return []

    created_s = None
    if isinstance(job, dict) and job.get("created"):
        created_s = float(job["created"]) / 1000.0 - 1.0
    state_t = (merged_state or {}).get("t")
    state_s = (float(state_t) / 1000.0 - 1.0) if isinstance(state_t, (int, float)) and state_t > 0 else None

    anchors = [x for x in ([created_s, state_s] + bound_ts) if x is not None]
    min_s = min(anchors) if anchors else None
    max_s = None
    if isinstance(job, dict) and job.get("st") == "closed" and job.get("closed_at"):
        max_s = float(job["closed_at"]) / 1000.0 + 1.0

    assigned_to = (job.get("to") or "").strip().lower() if isinstance(job, dict) else ""
    out = []
    for r in unbound_cands:
        if assigned_to:
            who = str(r.get("surveyor") or r.get("by") or "").strip().lower()
            dev = str(r.get("device_id") or "").strip().lower()
            if who and who != assigned_to and dev != assigned_to:
                continue
        ts = _iso_to_seconds(r.get("taken") or r.get("t"))
        if ts is not None:
            if min_s is not None and ts < min_s:
                continue
            if max_s is not None and ts > max_s:
                continue
        out.append(r)
    return out


class ProgressDialog(QDialog):
    """Live checklist progress tree for a capture job, with missing-only filter and map selection."""

    def __init__(self, store, job_id, records=(), iface=None, layer=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.job_id = str(job_id or "").strip()
        self.records = list(records or ())
        self.iface = iface
        self._layer = layer
        self.unknown_job = False
        self.tree_model = None
        self.missing_items = []
        self.unbound_records = []
        self.duplicate_groups = []

        self.setWindowTitle(tr("Job progress"))
        self.resize(920, 560)

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        self.summary_lbl = QLabel()
        self.summary_lbl.setWordWrap(True)
        top.addWidget(self.summary_lbl, 1)

        self.missing_box = QCheckBox(tr("Missing only"))
        self.missing_box.toggled.connect(self._apply_filter)
        top.addWidget(self.missing_box)
        v.addLayout(top)

        self.tree = QTreeWidget()
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setHeaderLabels([
            tr("Path"),
            tr("Required"),
            tr("Photos (have/min)"),
            tr("Answers"),
            tr("State"),
            tr("On the phone, not here yet"),
        ])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        v.addWidget(self.tree, 1)

        hint = QLabel(tr("Double-click an item to select its photos on the map layer."))
        hint.setWordWrap(True)
        v.addWidget(hint)

        brow = QHBoxLayout()
        self.report_btn = QPushButton(tr("Report (PDF)…"))
        self.report_btn.clicked.connect(lambda: self.export_report())
        brow.addWidget(self.report_btn)
        brow.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        brow.addWidget(buttons)
        v.addLayout(brow)

        self.refresh()

    def export_report(self, path=None, open_after=True):
        return export_job_report_dialog(self.store, self.job_id, self.records, self.iface,
                                        parent=self, path=path, open_after=open_after)

    def set_records(self, records):
        self.records = list(records or ())
        self.refresh()

    @staticmethod
    def _format_answers(item):
        q_defs = item.get("q") or ()
        ans = item.get("answers") or {}
        missing_q = set(item.get("missing_q") or ())
        if not q_defs and not ans:
            return "—"
        parts = []
        yes_no = {True: tr("Yes"), False: tr("No")}
        seen = set()
        for q in q_defs:
            qname = q["name"]
            seen.add(qname)
            if qname in ans:
                val = ans[qname]
                sval = yes_no.get(val, val) if isinstance(val, bool) else val
                parts.append(f"{q['label']}: {sval}")
            elif qname in missing_q:
                parts.append(f"{q['label']}: ✗")
        for qname, val in ans.items():
            if qname not in seen:
                sval = yes_no.get(val, val) if isinstance(val, bool) else val
                parts.append(f"{qname}: {sval}")
        return "; ".join(parts) if parts else "—"

    @staticmethod
    def _format_state(item):
        if item.get("skipped"):
            return f"⏭ {tr('Skipped: {r}').format(r=item.get('skip') or '')}"
        if item.get("done"):
            return "✓"
        return "✗"

    def _collect_slot_binding(self, item):
        keys = set()
        pids = set()
        if item.get("k"):
            keys.add(item["k"])
        if item.get("path_label"):
            keys.add(item["path_label"])
        for pid in item.get("photo_ids") or ():
            pids.add(pid)
        for r in item.get("records") or ():
            if isinstance(r, dict) and r.get("id"):
                pids.add(r["id"])
        for sub in item.get("instances") or ():
            sk, sp = self._collect_slot_binding(sub)
            keys.update(sk)
            pids.update(sp)
        for ch in item.get("children") or ():
            ck, cp = self._collect_slot_binding(ch)
            keys.update(ck)
            pids.update(cp)
        return keys, pids

    def _build_tree_item(self, item):
        req_txt = tr("Yes") if item.get("req", True) else tr("No")
        if item["kind"] == "repeat":
            rmin = item["repeat"]["min"]
            photos_txt = f"{item['instance_count']}/{rmin}"
            ans_txt = "—"
            pending_n = sum(sub.get("pending_count", 0) for sub in item.get("instances") or ())
        else:
            pmin, pmax = item.get("photos") or (0, 0)
            have = item.get("photo_count", 0)
            photos_txt = f"{have}/{pmin}" if (pmax > 0 or have > 0) else "—"
            ans_txt = self._format_answers(item)
            pending_n = int(item.get("pending_count", 0))

        st_txt = self._format_state(item)
        pending_txt = str(pending_n)

        tw = QTreeWidgetItem([
            item.get("path_label") or item.get("label") or item.get("id") or "",
            req_txt,
            photos_txt,
            ans_txt,
            st_txt,
            pending_txt,
        ])
        slot_keys, photo_ids = self._collect_slot_binding(item)
        is_missing = bool(item.get("req", True) and not item.get("done") and not item.get("skipped"))
        meta = {
            "kind": item["kind"],
            "k": item.get("k") or "",
            "id": item.get("id") or "",
            "iid": item.get("iid") or "",
            "path_label": item.get("path_label") or "",
            "req": bool(item.get("req", True)),
            "done": bool(item.get("done")),
            "skipped": bool(item.get("skipped")),
            "skip": item.get("skip") or "",
            "missing": is_missing,
            "pending_count": pending_n,
            "slot_keys": slot_keys,
            "photo_ids": photo_ids,
        }
        tw.setData(0, Qt.ItemDataRole.UserRole, meta)
        if item.get("skipped"):
            tw.setForeground(4, QColor("#C98A12"))
        elif item.get("done"):
            tw.setForeground(4, QColor("#12A36E"))
        elif item.get("req", True):
            tw.setForeground(4, QColor("#D24B4B"))

        for sub in item.get("instances") or ():
            tw.addChild(self._build_tree_item(sub))
        for ch in item.get("children") or ():
            tw.addChild(self._build_tree_item(ch))
        return tw

    def refresh(self):
        self.store.reload()
        job = self.store.get(self.job_id)
        merged = self.store.merged_state(self.job_id)
        schema, is_unknown = resolve_job_schema(self.store, self.job_id, merged, self.records)
        self.unknown_job = is_unknown
        self.tree_model = tree(schema, merged, self.records)
        self.missing_items = missing(self.tree_model)
        pc = progress_counts(self.tree_model)
        self.unbound_records = unbound_job_records(job, self.job_id, merged, self.records)
        self.duplicate_groups = duplicate_instances(self.tree_model)

        title = (job.get("title") if job else None) or self.job_id
        if is_unknown:
            title_part = f"{title} — {tr('Unknown job')}"
        else:
            st_lbl = tr("In progress") if (job or {}).get("st", "open") == "open" else tr("Closed")
            title_part = f"{title} ({self.job_id}) — {st_lbl}"
        self.setWindowTitle(f"{tr('Job progress')} — {title_part}")

        miss_n = len(self.missing_items)
        prog_part = tr("Complete") if miss_n == 0 else tr("{n} missing").format(n=miss_n)
        self.summary_lbl.setText(f"{title_part} · {pc['percent']}% ({prog_part})")

        self.tree.clear()
        for root_node in self.tree_model["roots"]:
            self.tree.addTopLevelItem(self._build_tree_item(root_node))

        # Extra photos bucket (cap.k == '*')
        extra_recs = self.tree_model.get("extra_records") or []
        extra_pids = self.tree_model.get("extra_photo_ids") or []
        if extra_recs or extra_pids:
            rec_ids = {r["id"] for r in extra_recs if isinstance(r, dict) and r.get("id")}
            pend_extra = len([pid for pid in extra_pids if pid not in rec_ids])
            ext_it = QTreeWidgetItem([
                tr("Extra photo for this job"),
                tr("No"),
                str(len(extra_pids)),
                "—",
                "✓",
                str(pend_extra),
            ])
            ext_it.setData(0, Qt.ItemDataRole.UserRole, {
                "kind": "extra",
                "k": "*",
                "path_label": "*",
                "req": False,
                "done": True,
                "skipped": False,
                "missing": False,
                "pending_count": pend_extra,
                "slot_keys": {"*"},
                "photo_ids": set(extra_pids),
            })
            self.tree.addTopLevelItem(ext_it)

        # Bucket for photos without an item during this job (old page)
        if self.unbound_records:
            ub_pids = {r["id"] for r in self.unbound_records if isinstance(r, dict) and r.get("id")}
            ub_it = QTreeWidgetItem([
                tr("Photos without an item during this job ({n})").format(n=len(self.unbound_records)),
                "—",
                str(len(self.unbound_records)),
                "—",
                "!",
                "0",
            ])
            ub_it.setData(0, Qt.ItemDataRole.UserRole, {
                "kind": "unbound",
                "k": "",
                "path_label": tr("Photos without an item during this job ({n})").format(n=len(self.unbound_records)),
                "req": False,
                "done": False,
                "skipped": False,
                "missing": True,
                "pending_count": 0,
                "slot_keys": set(),
                "photo_ids": ub_pids,
            })
            for r in self.unbound_records:
                ch = QTreeWidgetItem([
                    r.get("id") or "",
                    "—",
                    "1",
                    r.get("note") or "—",
                    "!",
                    "0",
                ])
                ch.setData(0, Qt.ItemDataRole.UserRole, {
                    "kind": "unbound_photo",
                    "k": "",
                    "path_label": r.get("id") or "",
                    "req": False,
                    "done": False,
                    "skipped": False,
                    "missing": True,
                    "pending_count": 0,
                    "slot_keys": set(),
                    "photo_ids": {r["id"]} if r.get("id") else set(),
                })
                ub_it.addChild(ch)
            self.tree.addTopLevelItem(ub_it)

        # Bucket for duplicate instances made offline by two surveyors
        if self.duplicate_groups:
            dup_keys = set()
            dup_pids = set()
            dup_it = QTreeWidgetItem([
                tr("Duplicate instances ({n})").format(n=len(self.duplicate_groups)),
                "—",
                str(sum(len(g["instances"]) for g in self.duplicate_groups)),
                "—",
                "!",
                "0",
            ])
            for g in self.duplicate_groups:
                for inst_item in g["instances"]:
                    sk, sp = self._collect_slot_binding(inst_item)
                    dup_keys.update(sk)
                    dup_pids.update(sp)
                    ch = self._build_tree_item(inst_item)
                    ch.setText(0, f"{inst_item['path_label']} [{inst_item['iid']}]")
                    ch_meta = dict(ch.data(0, Qt.ItemDataRole.UserRole) or {}, kind="duplicate_instance", missing=True)
                    ch.setData(0, Qt.ItemDataRole.UserRole, ch_meta)
                    dup_it.addChild(ch)
            dup_it.setData(0, Qt.ItemDataRole.UserRole, {
                "kind": "duplicates",
                "k": "",
                "path_label": tr("Duplicate instances ({n})").format(n=len(self.duplicate_groups)),
                "req": False,
                "done": False,
                "skipped": False,
                "missing": True,
                "pending_count": 0,
                "slot_keys": dup_keys,
                "photo_ids": dup_pids,
            })
            self.tree.addTopLevelItem(dup_it)

        self.tree.expandAll()
        self._apply_filter()

    def _filter_tree_item(self, tw, only_missing):
        if not only_missing:
            tw.setHidden(False)
            for i in range(tw.childCount()):
                self._filter_tree_item(tw.child(i), False)
            return True
        meta = tw.data(0, Qt.ItemDataRole.UserRole) or {}
        child_visible = False
        for i in range(tw.childCount()):
            if self._filter_tree_item(tw.child(i), True):
                child_visible = True
        own_missing = bool(meta.get("missing"))
        show = own_missing or child_visible
        tw.setHidden(not show)
        return show

    def _apply_filter(self, *_a):
        only_missing = self.missing_box.isChecked()
        for i in range(self.tree.topLevelItemCount()):
            self._filter_tree_item(self.tree.topLevelItem(i), only_missing)

    def visible_keys(self):
        """Return the slot keys `k` (or kind for buckets) of all currently visible items in the tree."""
        out = []

        def _walk(tw):
            if tw.isHidden():
                return
            meta = tw.data(0, Qt.ItemDataRole.UserRole) or {}
            out.append(meta.get("k") or meta.get("kind") or tw.text(0))
            for i in range(tw.childCount()):
                _walk(tw.child(i))

        for i in range(self.tree.topLevelItemCount()):
            _walk(self.tree.topLevelItem(i))
        return out

    def _photos_layer(self):
        if self._layer is not None and isinstance(self._layer, QgsVectorLayer) and self._layer.isValid():
            return self._layer
        gpkg_norm = os.path.normcase(os.path.join(self.store.folder, "field_photos.gpkg"))
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsVectorLayer) and lyr.isValid():
                src = os.path.normcase(lyr.source().split("|")[0])
                if src == gpkg_norm or (lyr.fields().indexOf("job_id") >= 0 and lyr.fields().indexOf("job_item") >= 0):
                    return lyr
        if os.path.isfile(os.path.join(self.store.folder, "field_photos.gpkg")):
            from .dock import open_layer

            return open_layer(self.store.folder, add=False)
        return None

    def select_item_photos(self, item_or_key):
        """Select features on the photos layer matching a QTreeWidgetItem or slot key `k`."""
        lyr = self._photos_layer()
        if lyr is None:
            return []
        if isinstance(item_or_key, QTreeWidgetItem):
            meta = item_or_key.data(0, Qt.ItemDataRole.UserRole) or {}
        elif isinstance(item_or_key, dict):
            meta = item_or_key
        else:
            k_str = str(item_or_key or "")
            meta = {"k": k_str, "slot_keys": {k_str}, "photo_ids": set()}

        slot_keys = set(meta.get("slot_keys") or ())
        if meta.get("k"):
            slot_keys.add(meta["k"])
        photo_ids = set(meta.get("photo_ids") or ())
        kind = meta.get("kind") or ""

        j_idx = lyr.fields().indexOf("job_id")
        ji_idx = lyr.fields().indexOf("job_item")
        p_idx = lyr.fields().indexOf("photo_id")

        fids = []
        for f in lyr.getFeatures():
            pid = f["photo_id"] if p_idx >= 0 else None
            jid = f["job_id"] if j_idx >= 0 else None
            jitem = f["job_item"] if ji_idx >= 0 else None
            if kind in ("unbound", "unbound_photo"):
                if pid in photo_ids:
                    fids.append(f.id())
            else:
                if (jid == self.job_id and jitem in slot_keys) or (pid and pid in photo_ids):
                    fids.append(f.id())

        lyr.selectByIds(fids)
        if self.iface is not None and hasattr(self.iface, "mapCanvas") and self.iface.mapCanvas() is not None:
            self.iface.mapCanvas().refresh()
        return fids

    def _on_item_double_clicked(self, item, _col):
        self.select_item_photos(item)
