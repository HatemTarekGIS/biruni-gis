"""Capture jobs store and office dialog: create jobs from templates, freeze their schema, send to the guide."""
import copy
import json
import os
import threading

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject
from qgis.PyQt.QtCore import QDate, Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
                                 QDoubleSpinBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                                 QMessageBox, QPushButton, QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                                 QWidget)

from ..i18n import tr
from .capture import JOB_ID_RE, MAX_CAP, cap_payload, clean_schema, clean_state, merge_states, new_job, stats
from .capture_dialog import SchemaDialog, load_office_schemas


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

    def save_state(self, raw_state):
        """Save a device state snapshot (`state-<dev>.json`) if its rev is newer."""
        with self.lock:
            job_id = str((raw_state or {}).get("job") or "")
            job = self.jobs.get(job_id)
            schema = job["schema"] if job else None
            st = clean_state(raw_state, schema)
            jdir = os.path.join(self.jobs_dir, st["job"])
            os.makedirs(jdir, exist_ok=True)
            spath = os.path.join(jdir, f"state-{st['dev']}.json")
            if os.path.isfile(spath):
                try:
                    old = json.load(open(spath, encoding="utf-8"))
                    if isinstance(old, dict) and int(old.get("rev", -1)) >= st["rev"]:
                        return old, False
                except (OSError, ValueError):
                    pass
            part = spath + ".part"
            with open(part, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(part, spath)
            return st, True

    def states(self, job_id):
        jdir = os.path.join(self.jobs_dir, job_id)
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
        return merge_states(self.states(job_id), schema)

    def payload(self, limit=MAX_CAP):
        """Build the `guide.cap` payload for all open jobs."""
        return cap_payload(self.list_jobs(status="open"), limit=limit)


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
        v.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.new_btn = QPushButton(tr("New job…"))
        self.new_btn.clicked.connect(self._new_job)
        self.tpl_btn = QPushButton(tr("Templates…"))
        self.tpl_btn.clicked.connect(self._edit_templates)
        self.close_btn = QPushButton(tr("Close job"))
        self.close_btn.clicked.connect(self._close_selected)
        self.reopen_btn = QPushButton(tr("Reopen job"))
        self.reopen_btn.clicked.connect(self._reopen_selected)
        for b in (self.new_btn, self.tpl_btn, self.close_btn, self.reopen_btn):
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

    def refresh(self):
        self.table.clear()
        for j in self.store.list_jobs():
            merged = self.store.merged_state(j["id"])
            st_info = stats(j["schema"], merged, self.records)
            miss_n = len(st_info["missing"])
            prog = tr("Complete") if miss_n == 0 else tr("{n} missing").format(n=miss_n)
            status_txt = tr("Open") if j.get("st", "open") == "open" else tr("Closed")
            it = QTreeWidgetItem([
                j["id"],
                j.get("title") or j["id"],
                f"{j['schema']['title']} ({j['s']})",
                status_txt,
                prog,
                j.get("to") or "—",
            ])
            it.setData(0, Qt.ItemDataRole.UserRole, j["id"])
            self.table.addTopLevelItem(it)
        if self.table.topLevelItemCount() > 0:
            self.table.setCurrentItem(self.table.topLevelItem(0))

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
