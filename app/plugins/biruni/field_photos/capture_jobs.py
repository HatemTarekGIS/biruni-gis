"""Capture jobs store and office dialog: create jobs from templates, freeze their schema, send to the guide."""
import copy
import json
import os
import re
import threading

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject
from qgis.PyQt.QtCore import QDate, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog,
                                 QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
                                 QLabel, QLineEdit, QMessageBox, QProgressDialog, QPushButton, QSpinBox, QTreeWidget,
                                 QTreeWidgetItem, QVBoxLayout, QWidget)

from ..i18n import tr
from .capture import (DEV_RE, JOB_ID_RE, MAX_CAP, NODE_ID_RE, cap_payload, clean_cap, clean_schema, clean_state,
                      merge_states, new_job, progress_counts, schema_key, stats)

ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def resolve_job_schema(store, job_id, merged=None, records=()):
    """Return the frozen schema for a known job, or resolve/synthesize one for an unknown job."""
    from .capture_dialog import load_office_schemas

    job = store.get(job_id) if store is not None else None
    if job and isinstance(job.get("schema"), dict):
        return job["schema"], False
    s_ref = str((merged or {}).get("s") or "")
    office = load_office_schemas()
    if s_ref:
        for s in office:
            if schema_key(s) == s_ref:
                return s, True
        sid = s_ref.split("@", 1)[0]
        for s in office:
            if s["id"] == sid:
                return s, True

    # Synthesize a fallback schema from state instances, answers, photos, skips, and photo records
    nodes_by_id = {}
    parent_of = {}
    root_ids = []

    def _ensure_node(nid, is_repeat=False, has_photos=False):
        if not NODE_ID_RE.fullmatch(str(nid or "")):
            return None
        n = nodes_by_id.get(nid)
        if n is None:
            n = {"id": nid, "label": nid.replace("_", " ").title(), "req": True}
            nodes_by_id[nid] = n
        if is_repeat and "repeat" not in n:
            n["repeat"] = {"min": 1, "max": 200}
        if has_photos and "photos" not in n:
            n["photos"] = [1, 50]
        return n

    inst = (merged or {}).get("inst") or {}
    for iid, info in inst.items():
        if not isinstance(info, dict):
            continue
        nid = info.get("n")
        if not _ensure_node(nid, is_repeat=True):
            continue
        p_iid = info.get("p") or ""
        if p_iid and p_iid in inst:
            p_nid = inst[p_iid].get("n")
            if p_nid and p_nid != nid and nid not in parent_of:
                parent_of[nid] = p_nid
        elif nid not in root_ids:
            root_ids.append(nid)

    for r in records or ():
        cap = clean_cap(r.get("cap") if isinstance(r, dict) else None)
        if not cap or cap["j"] != job_id:
            continue
        prev_nid = None
        for nid, _iid, _ord in cap.get("ch") or ():
            if not _ensure_node(nid, is_repeat=True):
                continue
            if prev_nid and prev_nid != nid and nid not in parent_of:
                parent_of[nid] = prev_nid
            elif not prev_nid and nid not in root_ids:
                root_ids.append(nid)
            prev_nid = nid
        k = cap["k"]
        if k != "*" and ":" in k:
            iid_part, leaf_nid = k.split(":", 1)
            if _ensure_node(leaf_nid, is_repeat=bool(iid_part and not prev_nid), has_photos=True):
                if prev_nid and prev_nid != leaf_nid and leaf_nid not in parent_of:
                    parent_of[leaf_nid] = prev_nid
                elif leaf_nid not in parent_of and leaf_nid not in root_ids:
                    root_ids.append(leaf_nid)

    for mapping_key, has_ph in (("ph", True), ("ans", False), ("skip", False), ("note", False)):
        for k, val in ((merged or {}).get(mapping_key) or {}).items():
            if k == "*" or ":" not in str(k):
                continue
            iid_part, nid = str(k).split(":", 1)
            n = _ensure_node(nid, is_repeat=bool(iid_part and iid_part in inst), has_photos=has_ph)
            if n is None:
                continue
            if iid_part and iid_part in inst:
                p_nid = inst[iid_part].get("n")
                if p_nid and p_nid != nid and nid not in parent_of:
                    parent_of[nid] = p_nid
            if nid not in parent_of and nid not in root_ids:
                root_ids.append(nid)
            if mapping_key == "ans" and isinstance(val, dict):
                existing_q = {q["name"] for q in (n.get("q") or ())}
                q_list = list(n.get("q") or ())
                for qname in val:
                    if qname not in existing_q:
                        q_list.append({"name": qname, "label": qname, "type": "text", "req": False})
                        existing_q.add(qname)
                if q_list:
                    n["q"] = q_list

    for nid, p_nid in list(parent_of.items()):
        p_node = nodes_by_id.get(p_nid)
        c_node = nodes_by_id.get(nid)
        if p_node and c_node and p_nid != nid:
            ch_list = p_node.setdefault("nodes", [])
            if c_node not in ch_list:
                ch_list.append(c_node)

    roots = [nodes_by_id[nid] for nid in root_ids if nid in nodes_by_id and nid not in parent_of]
    for nid, n in nodes_by_id.items():
        if nid not in parent_of and n not in roots:
            roots.append(n)
    if not roots:
        roots = [{"id": "item", "label": tr("Checklist item"), "req": True, "photos": [1, 10]}]

    raw_s = {
        "biruni_capture": 1,
        "id": "unknown",
        "rev": 1,
        "title": tr("Unknown job"),
        "job": [],
        "nodes": roots,
    }
    return clean_schema(raw_s), True


class JobStore:
    """Stores jobs and per-device state files under `<photo_folder>/jobs/<jobId>/`."""

    def __init__(self, folder):
        self.folder = folder
        self.jobs_dir = os.path.join(folder, "jobs")
        self.lock = threading.Lock()
        self.jobs = {}
        self.reload()

    def reload(self):
        with self.lock:
            self.jobs = {}
            if not os.path.isdir(self.jobs_dir):
                return
            for jid in sorted(os.listdir(self.jobs_dir)):
                if not JOB_ID_RE.match(jid):
                    continue
                jpath = os.path.join(self.jobs_dir, jid, "job.json")
                if not os.path.isfile(jpath):
                    continue
                try:
                    with open(jpath, encoding="utf-8") as f:
                        d = json.load(f)
                    if isinstance(d, dict) and d.get("id") == jid and isinstance(d.get("schema"), dict):
                        d["schema"] = clean_schema(d["schema"])
                        self.jobs[jid] = d
                except (OSError, ValueError):
                    continue

    def _write_job(self, job):
        jdir = os.path.join(self.jobs_dir, job["id"])
        os.makedirs(jdir, exist_ok=True)
        path = os.path.join(jdir, "job.json")
        part = path + ".part"
        with open(part, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, path)

    def create(self, schema, data=None, title="", loc=None, to="", job_id=None):
        """Create a job with a frozen schema snapshot and write `jobs/<jobId>/job.json`."""
        job = new_job(copy.deepcopy(schema), data=data, title=title, loc=loc, to=to, job_id=job_id)
        with self.lock:
            self._write_job(job)
            self.jobs[job["id"]] = job
        return copy.deepcopy(job)

    def get(self, job_id):
        with self.lock:
            j = self.jobs.get(job_id)
            return copy.deepcopy(j) if j else None

    def list_jobs(self, status=None):
        with self.lock:
            items = list(self.jobs.values())
        items.sort(key=lambda j: (0 if j.get("st") == "open" else 1, -(j.get("created") or 0), j["id"]))
        if status is not None:
            items = [j for j in items if j.get("st", "open") == status]
        return copy.deepcopy(items)

    def set_status(self, job_id, st):
        st = "closed" if st == "closed" else "open"
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            job["st"] = st
            self._write_job(job)
            return copy.deepcopy(job)

    def close_job(self, job_id):
        return self.set_status(job_id, "closed")

    def reopen_job(self, job_id):
        return self.set_status(job_id, "open")

    def _safe_job_dir(self, job_id):
        if not isinstance(job_id, str):
            raise ValueError("bad job id")
        jid = job_id.strip()
        if not JOB_ID_RE.fullmatch(jid):
            raise ValueError(f"bad job id: {job_id!r}")
        base = os.path.abspath(self.jobs_dir)
        jdir = os.path.abspath(os.path.join(base, jid))
        if os.path.dirname(jdir) != base:
            raise ValueError("unsafe job path")
        return jid, jdir

    def _safe_state_path(self, job_id, dev):
        jid, jdir = self._safe_job_dir(job_id)
        if not isinstance(dev, str):
            raise ValueError("bad dev id")
        did = dev.strip()
        if not DEV_RE.fullmatch(did):
            raise ValueError(f"bad dev id: {dev!r}")
        spath = os.path.abspath(os.path.join(jdir, f"state-{did}.json"))
        if os.path.dirname(spath) != jdir:
            raise ValueError("unsafe state path")
        return jid, did, jdir, spath

    @staticmethod
    def _atomic_write_json(path, obj):
        part = path + ".part"
        with open(part, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, path)

    def take_state(self, job_id, dev, state, item_id=None):
        """Keep the highest rev per (job_id, dev) in `jobs/<jobId>/state-<dev>.json`; ignore older or replayed snapshots."""
        if not isinstance(state, dict):
            raise ValueError("state must be an object")
        jid, did, jdir, spath = self._safe_state_path(job_id, dev)
        if state.get("job") not in (None, "") and str(state.get("job")).strip() != jid:
            raise ValueError("job id mismatch")
        if state.get("dev") not in (None, "") and str(state.get("dev")).strip() != did:
            raise ValueError("dev id mismatch")
        iid = str(item_id or state.get("id") or "").strip()
        if iid and not ITEM_ID_RE.fullmatch(iid):
            iid = ""
        with self.lock:
            job = self.jobs.get(jid)
            schema = job["schema"] if job else None
            raw = dict(state, job=jid, dev=did)
            st = clean_state(raw, schema)
            prev_ids = []
            if os.path.isfile(spath):
                try:
                    with open(spath, encoding="utf-8") as f:
                        old = json.load(f)
                    if isinstance(old, dict):
                        prev_ids = [x for x in (old.get("ids") or ()) if isinstance(x, str)]
                        if isinstance(old.get("id"), str) and old["id"] not in prev_ids:
                            prev_ids.append(old["id"])
                        if int(old.get("rev", -1)) >= st["rev"]:
                            if iid and iid not in prev_ids:
                                old["ids"] = (prev_ids + [iid])[-200:]
                                self._atomic_write_json(spath, old)
                            return old, False
                except (OSError, ValueError):
                    pass
            if iid and iid not in prev_ids:
                prev_ids.append(iid)
            if prev_ids:
                st["ids"] = prev_ids[-200:]
            os.makedirs(jdir, exist_ok=True)
            self._atomic_write_json(spath, st)
            return st, True

    def save_state(self, raw_state):
        """Save a device state snapshot (`state-<dev>.json`) if its rev is newer."""
        if not isinstance(raw_state, dict):
            raise ValueError("state must be an object")
        return self.take_state(raw_state.get("job"), raw_state.get("dev"), raw_state, item_id=raw_state.get("id"))

    def state_ids(self):
        """Return all state item ids recorded across `jobs/*/state-*.json` (for Drive cleanup receipts)."""
        out = set()
        if not os.path.isdir(self.jobs_dir):
            return out
        for jid in os.listdir(self.jobs_dir):
            if not JOB_ID_RE.fullmatch(jid):
                continue
            jdir = os.path.join(self.jobs_dir, jid)
            if not os.path.isdir(jdir):
                continue
            for name in os.listdir(jdir):
                if name.startswith("state-") and name.endswith(".json"):
                    try:
                        with open(os.path.join(jdir, name), encoding="utf-8") as f:
                            d = json.load(f)
                        if isinstance(d, dict):
                            if isinstance(d.get("id"), str) and d["id"]:
                                out.add(d["id"])
                            for x in d.get("ids") or ():
                                if isinstance(x, str) and x:
                                    out.add(x)
                    except (OSError, ValueError):
                        continue
        return out

    def unknown_job_ids(self, records=()):
        """List safe job IDs that have `state-*.json` files or bound photo records but no `job.json`."""
        with self.lock:
            known = set(self.jobs)
        found = set()
        if os.path.isdir(self.jobs_dir):
            for jid in sorted(os.listdir(self.jobs_dir)):
                if not JOB_ID_RE.fullmatch(jid) or jid in known:
                    continue
                jdir = os.path.join(self.jobs_dir, jid)
                if not os.path.isdir(jdir) or os.path.isfile(os.path.join(jdir, "job.json")):
                    continue
                if any(n.startswith("state-") and n.endswith(".json") for n in os.listdir(jdir)):
                    found.add(jid)
        for r in records or ():
            cap = clean_cap(r.get("cap") if isinstance(r, dict) else None)
            if cap and cap["j"] not in known:
                found.add(cap["j"])
        return sorted(found)

    def states(self, job_id):
        try:
            _jid, jdir = self._safe_job_dir(job_id)
        except ValueError:
            return []
        if not os.path.isdir(jdir):
            return []
        out = []
        for name in sorted(os.listdir(jdir)):
            if name.startswith("state-") and name.endswith(".json"):
                try:
                    with open(os.path.join(jdir, name), encoding="utf-8") as f:
                        out.append(json.load(f))
                except (OSError, ValueError):
                    continue
        return out

    def merged_state(self, job_id):
        job = self.get(job_id)
        schema = job["schema"] if job else None
        m = merge_states(self.states(job_id), schema)
        if not m.get("job") and isinstance(job_id, str) and JOB_ID_RE.fullmatch(job_id.strip()):
            m["job"] = job_id.strip()
        return m

    def payload(self, limit=MAX_CAP):
        """Build the `guide.cap` payload for all open jobs."""
        return cap_payload(self.list_jobs(status="open"), limit=limit)


def export_job_report_dialog(store, job_id, records=(), iface=None, parent=None, path=None, open_after=True):
    """Build and write a sealed PDF report for `job_id` with a cancellable QProgressDialog."""
    import datetime as dt
    from .capture_report import render_location_map, report_model, write_report

    if not job_id:
        return None
    store.reload()
    job = store.get(job_id)
    merged = store.merged_state(job_id)
    schema, _ = resolve_job_schema(store, job_id, merged, records)
    if job is None:
        job = {"id": job_id, "title": f"{tr('Unknown job')} ({job_id})", "schema": schema, "s": schema_key(schema),
               "data": {}, "st": "open"}
    else:
        job = dict(job, schema=schema)

    try:
        _jid, default_dir = store._safe_job_dir(job_id)
    except ValueError:
        default_dir = store.folder
    os.makedirs(default_dir, exist_ok=True)
    today = dt.date.today().isoformat()
    default_path = os.path.join(default_dir, f"report-{today}.pdf")

    if path is None:
        path, _ = QFileDialog.getSaveFileName(parent, tr("Save report"), default_path, "PDF (*.pdf)")
    if not path:
        return None

    model = report_model(job, merged, records, folder=store.folder)
    dlg = QProgressDialog(tr("Writing the PDF…"), tr("Cancel"), 0, 100, parent)
    dlg.setWindowTitle(tr("Capture Job Report"))
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setValue(0)
    QApplication.processEvents()

    map_img = render_location_map(model, iface=iface)

    def _on_progress(step, total):
        if dlg.wasCanceled():
            return False
        pct = int(round(100.0 * step / max(total, 1)))
        dlg.setValue(min(99, pct))
        QApplication.processEvents()
        return not dlg.wasCanceled()

    try:
        res = write_report(path, model, map_image=map_img, progress=_on_progress)
    finally:
        dlg.setValue(100)
        dlg.close()

    if res.get("cancelled"):
        return res
    if open_after and os.path.isfile(path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
    return res


def _canvas_center_wgs84(iface):
    if iface is None or not hasattr(iface, "mapCanvas") or iface.mapCanvas() is None:
        return None
    canvas = iface.mapCanvas()
    ext = canvas.extent()
    if ext.isEmpty():
        return None
    c = ext.center()
    crs = canvas.mapSettings().destinationCrs()
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    if crs.isValid() and crs != wgs84:
        try:
            c = QgsCoordinateTransform(crs, wgs84, QgsProject.instance()).transform(QgsPointXY(c.x(), c.y()))
        except Exception:  # noqa: BLE001
            return None
    if -180 <= c.x() <= 180 and -90 <= c.y() <= 90:
        return [round(c.x(), 7), round(c.y(), 7)]
    return None


class NewJobDialog(QDialog):
    """Dialog to create a new job from a template, fill in job questions, and pick its location."""

    def __init__(self, schemas, iface=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("New job"))
        self.resize(500, 420)
        self.iface = iface
        self.schemas = list(schemas)
        self._q_widgets = {}

        v = QVBoxLayout(self)
        top = QFormLayout()
        self.tpl_combo = QComboBox()
        for s in self.schemas:
            self.tpl_combo.addItem(f"{s['title']} ({s['id']}@{s['rev']})", s)
        self.tpl_combo.currentIndexChanged.connect(self._rebuild_questions)
        top.addRow(tr("Template:"), self.tpl_combo)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText(tr("Optional — defaults to the title question or job ID"))
        top.addRow(tr("Job title:"), self.title_edit)

        self.to_edit = QLineEdit()
        self.to_edit.setPlaceholderText(tr("Any surveyor"))
        top.addRow(tr("Assigned to:"), self.to_edit)

        lrow = QHBoxLayout()
        self.loc_box = QCheckBox(tr("Include location (lon, lat):"))
        self.lon_spin = QDoubleSpinBox()
        self.lon_spin.setRange(-180.0, 180.0)
        self.lon_spin.setDecimals(6)
        self.lat_spin = QDoubleSpinBox()
        self.lat_spin.setRange(-90.0, 90.0)
        self.lat_spin.setDecimals(6)
        map_btn = QPushButton(tr("From map"))
        map_btn.clicked.connect(self._take_from_map)
        lrow.addWidget(self.loc_box)
        lrow.addWidget(self.lon_spin)
        lrow.addWidget(self.lat_spin)
        lrow.addWidget(map_btn)
        top.addRow(tr("Location:"), lrow)
        v.addLayout(top)

        self.q_container = QWidget()
        self.q_form = QFormLayout(self.q_container)
        v.addWidget(self.q_container, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

        self._take_from_map()
        self._rebuild_questions()

    def _take_from_map(self):
        loc = _canvas_center_wgs84(self.iface)
        if loc:
            self.loc_box.setChecked(True)
            self.lon_spin.setValue(loc[0])
            self.lat_spin.setValue(loc[1])

    def current_schema(self):
        return self.tpl_combo.currentData()

    def _rebuild_questions(self, *_a):
        while self.q_form.rowCount():
            self.q_form.removeRow(0)
        self._q_widgets = {}
        s = self.current_schema() or {}
        for q in s.get("job") or ():
            t = q["type"]
            lbl = q["label"] + (" *" if q.get("req") else "")
            if t == "choice":
                w = QComboBox()
                if not q.get("req"):
                    w.addItem("", "")
                for c in q.get("choices") or ():
                    w.addItem(c, c)
            elif t == "integer":
                w = QSpinBox()
                w.setRange(int(q.get("min") if q.get("min") is not None else -1_000_000),
                           int(q.get("max") if q.get("max") is not None else 1_000_000))
            elif t == "number":
                w = QDoubleSpinBox()
                w.setRange(float(q.get("min") if q.get("min") is not None else -1e9),
                           float(q.get("max") if q.get("max") is not None else 1e9))
                w.setDecimals(2)
            elif t == "yesno":
                w = QCheckBox()
            elif t == "date":
                w = QDateEdit(QDate.currentDate())
                w.setDisplayFormat("yyyy-MM-dd")
                w.setCalendarPopup(True)
            else:
                w = QLineEdit()
            self._q_widgets[q["name"]] = (q, w)
            self.q_form.addRow(lbl, w)

    def values(self):
        out = {}
        for name, (q, w) in self._q_widgets.items():
            t = q["type"]
            if t == "choice":
                v = w.currentData()
            elif t in ("integer", "number"):
                v = w.value()
            elif t == "yesno":
                v = w.isChecked()
            elif t == "date":
                v = w.date().toString("yyyy-MM-dd")
            elif t == "multi":
                v = [x.strip() for x in w.text().replace("،", ",").split(",") if x.strip()]
            else:
                v = w.text().strip()
            if v not in ("", None, []):
                out[name] = v
        return out

    def location(self):
        return [round(self.lon_spin.value(), 7), round(self.lat_spin.value(), 7)] if self.loc_box.isChecked() else None

    def _ok(self):
        vals = self.values()
        s = self.current_schema() or {}
        for q in s.get("job") or ():
            if q.get("req") and vals.get(q["name"]) in (None, "", []):
                QMessageBox.warning(self, self.windowTitle(),
                                    tr("Please fill in \"{q}\".").format(q=q["label"]))
                return
        self.accept()


class JobsDialog(QDialog):
    """Manage office jobs: list, create from template, edit templates, close/reopen, and publish to phones."""

    def __init__(self, store, server=None, iface=None, records=(), on_changed=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Capture jobs"))
        self.resize(780, 460)
        self.store = store
        self.server = server
        self.iface = iface
        self.records = list(records)
        self.on_changed = on_changed
        self.last_error = ""
        self.progress_dlg = None

        v = QVBoxLayout(self)
        intro = QLabel(tr("Create field jobs from a capture template. Open jobs are sent to the phones with the guide; "
                          "each job keeps its own frozen copy of the template."))
        intro.setWordWrap(True)
        v.addWidget(intro)

        self.table = QTreeWidget()
        self.table.setRootIsDecorated(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setHeaderLabels([tr("ID"), tr("Title"), tr("Template"), tr("Status"), tr("Progress"), tr("Assigned to")])
        self.table.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemDoubleClicked.connect(lambda *_: self._open_progress())
        v.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.new_btn = QPushButton(tr("New job…"))
        self.new_btn.clicked.connect(self._new_job)
        self.tpl_btn = QPushButton(tr("Templates…"))
        self.tpl_btn.clicked.connect(self._edit_templates)
        self.prog_btn = QPushButton(tr("Progress…"))
        self.prog_btn.clicked.connect(self._open_progress)
        self.report_btn = QPushButton(tr("Report (PDF)…"))
        self.report_btn.clicked.connect(self._export_report)
        self.close_btn = QPushButton(tr("Close job"))
        self.close_btn.clicked.connect(self._close_selected)
        self.reopen_btn = QPushButton(tr("Reopen job"))
        self.reopen_btn.clicked.connect(self._reopen_selected)
        for b in (self.new_btn, self.tpl_btn, self.prog_btn, self.report_btn, self.close_btn, self.reopen_btn):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)

        self.status_lbl = QLabel()
        self.status_lbl.setWordWrap(True)
        v.addWidget(self.status_lbl)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        v.addWidget(buttons)

        self.refresh()

    def set_records(self, records):
        self.records = list(records or ())
        self.refresh()
        if self.progress_dlg is not None:
            try:
                self.progress_dlg.set_records(self.records)
            except RuntimeError:
                self.progress_dlg = None

    def refresh(self):
        cur_jid = self.selected_job_id()
        self.table.clear()
        self.store.reload()
        for j in self.store.list_jobs():
            merged = self.store.merged_state(j["id"])
            st_info = stats(j["schema"], merged, self.records)
            pc = progress_counts(j["schema"], merged, self.records)
            miss_n = len(st_info["missing"])
            pct = pc["percent"]
            detail = tr("Complete") if miss_n == 0 else tr("{n} missing").format(n=miss_n)
            prog = f"{pct}% ({detail})"
            status_txt = tr("In progress") if j.get("st", "open") == "open" else tr("Closed")
            it = QTreeWidgetItem([
                j["id"],
                j.get("title") or j["id"],
                f"{j['schema']['title']} ({j['s']})",
                status_txt,
                prog,
                j.get("to") or "—",
            ])
            it.setData(0, Qt.ItemDataRole.UserRole, j["id"])
            it.setData(4, Qt.ItemDataRole.UserRole, pct)
            self.table.addTopLevelItem(it)
        for ujid in self.store.unknown_job_ids(self.records):
            merged = self.store.merged_state(ujid)
            schema, _ = resolve_job_schema(self.store, ujid, merged, self.records)
            pc = progress_counts(schema, merged, self.records)
            miss_n = pc["missing"]
            pct = pc["percent"]
            detail = tr("Complete") if miss_n == 0 else tr("{n} missing").format(n=miss_n)
            prog = f"{pct}% ({detail})"
            s_ref = merged.get("s") or f"{schema['id']}@{schema['rev']}"
            it = QTreeWidgetItem([
                ujid,
                f"{tr('Unknown job')} ({ujid})",
                f"{schema['title']} ({s_ref})",
                tr("Unknown job"),
                prog,
                "—",
            ])
            it.setData(0, Qt.ItemDataRole.UserRole, ujid)
            it.setData(4, Qt.ItemDataRole.UserRole, pct)
            self.table.addTopLevelItem(it)
        if self.table.topLevelItemCount() > 0:
            picked = self.table.topLevelItem(0)
            if cur_jid:
                for i in range(self.table.topLevelItemCount()):
                    cand = self.table.topLevelItem(i)
                    if cand.data(0, Qt.ItemDataRole.UserRole) == cur_jid:
                        picked = cand
                        break
            self.table.setCurrentItem(picked)

    def _open_progress(self):
        jid = self.selected_job_id()
        if not jid:
            return None
        from .capture_progress import ProgressDialog

        dlg = ProgressDialog(self.store, jid, self.records, self.iface, parent=self)
        self.progress_dlg = dlg
        dlg.exec()
        self.progress_dlg = None
        return dlg

    def _export_report(self, path=None, open_after=True):
        jid = self.selected_job_id()
        if not jid:
            return None
        res = export_job_report_dialog(self.store, jid, self.records, self.iface, parent=self,
                                       path=path, open_after=open_after)
        if res and not res.get("cancelled"):
            self.status_lbl.setText(tr("Saved: {p}").format(p=res["pdf"]))
        return res

    def selected_job_id(self):
        it = self.table.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it is not None else None

    def publish(self, limit=MAX_CAP):
        """Send open jobs to the server guide; refuse and restore if over the 60 KB limit."""
        try:
            payload = self.store.payload(limit=limit)
        except ValueError:
            self.last_error = tr("Too many open jobs to send to the phones (exceeds {kb} KB). Close finished jobs first.").format(
                kb=limit // 1000
            )
            self.status_lbl.setText(self.last_error)
            return False
        self.last_error = ""
        if self.server is not None:
            self.server.set_capture(payload)
        if self.on_changed is not None:
            self.on_changed(payload)
        n = len(payload["jobs"]) if payload else 0
        self.status_lbl.setText(tr("{n} open jobs ready for the phones.").format(n=n) if n else tr("No open jobs."))
        return True

    def create_job(self, schema, data=None, title="", loc=None, to="", limit=MAX_CAP):
        """Programmatic job creation that checks the cap payload limit before keeping the job open."""
        job = self.store.create(schema, data=data, title=title, loc=loc, to=to)
        if not self.publish(limit=limit):
            self.store.close_job(job["id"])
            self.refresh()
            return None
        self.refresh()
        return job

    def _new_job(self):
        from .capture_dialog import load_office_schemas

        schemas = load_office_schemas()
        dlg = NewJobDialog(schemas, iface=self.iface, parent=self)
        if dlg.exec():
            job = self.create_job(
                dlg.current_schema(),
                data=dlg.values(),
                title=dlg.title_edit.text().strip(),
                loc=dlg.location(),
                to=dlg.to_edit.text().strip(),
            )
            if job is None and self.last_error:
                QMessageBox.warning(self, self.windowTitle(), self.last_error)

    def _edit_templates(self):
        from .capture_dialog import SchemaDialog, load_office_schemas

        schemas = load_office_schemas()
        dlg = SchemaDialog(schemas[0] if schemas else None, self)
        dlg.exec()

    def _close_selected(self):
        jid = self.selected_job_id()
        if not jid:
            return
        self.store.close_job(jid)
        self.publish()
        self.refresh()

    def _reopen_selected(self):
        jid = self.selected_job_id()
        if not jid:
            return
        self.store.reopen_job(jid)
        if not self.publish():
            self.store.close_job(jid)
            if self.last_error:
                QMessageBox.warning(self, self.windowTitle(), self.last_error)
        self.refresh()
