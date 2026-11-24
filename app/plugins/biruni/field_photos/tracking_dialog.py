"""Office tracking policy dialog (no-loss P5: tracking_dialog.py).

Opened from the Field Photos dock button "Tracking…". Lets the office configure:
- the default tracking mode (off, with photos only, every 15/5/1 minutes, live),
- optional weekly office work hours (7 rows, Sunday=0 to Saturday=6),
- per-surveyor overrides (from `surveyor_keys.json`, keyed by surveyor ID and published as `sk_tag(secret)`),
- per-open-job overrides (from `JobStore.list_jobs(status="open")`),
and shows the fixed honesty note about web page tracking limitations.
"""
from qgis.PyQt.QtCore import QTime, Qt
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QGridLayout, QGroupBox,
                                 QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                                 QTimeEdit, QVBoxLayout)

from ..i18n import tr
from . import policy

DAY_NAMES = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)

MODE_CHOICES = (
    ("off", "Off"),
    ("photo", "With photos only"),
    ("15", "Every 15 minutes"),
    ("5", "Every 5 minutes"),
    ("1", "Every 1 minute"),
    ("live", "Live"),
)


def mode_label(mode):
    for k, label in MODE_CHOICES:
        if k == mode:
            return tr(label)
    return str(mode or "—")


def _min_to_qtime(m):
    m = max(0, min(1439, int(m)))
    return QTime(m // 60, m % 60)


def _qtime_to_min(qt, is_end=False):
    m = qt.hour() * 60 + qt.minute()
    if is_end and m == 0 and qt.second() == 0:
        return 1440
    return m


class TrackingDialog(QDialog):
    """Non-modal office dialog for configuring the signed room tracking policy (BRPL1)."""

    def __init__(self, dock, parent=None):
        super().__init__(parent or dock)
        self.dock = dock
        self.setObjectName("BiruniTrackingPolicy")
        self.setWindowTitle(tr("Tracking policy"))
        self.setMinimumWidth(560)

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)

        self.note_lbl = QLabel(
            tr("A web page cannot send positions while the phone is locked or the page is closed. "
               "You will see the time each surveyor was last seen.")
        )
        self.note_lbl.setWordWrap(True)
        self.note_lbl.setObjectName("GpsHint")
        v.addWidget(self.note_lbl)

        top_form = QFormLayout()
        self.mode_combo = QComboBox()
        for code, text in MODE_CHOICES:
            self.mode_combo.addItem(tr(text), code)
        top_form.addRow(tr("Default tracking mode:"), self.mode_combo)
        v.addLayout(top_form)

        # 7-row work hours table
        self.hours_box = QGroupBox(tr("Office work hours (optional — outside these hours tracking turns off)"))
        hv = QVBoxLayout(self.hours_box)
        self.use_hours_chk = QCheckBox(tr("Restrict tracking to office work hours"))
        hv.addWidget(self.use_hours_chk)

        self.hours_grid = QGridLayout()
        self.day_rows = []
        for d_idx, day_name in enumerate(DAY_NAMES):
            chk = QCheckBox(tr(day_name))
            t_from = QTimeEdit(QTime(8, 0))
            t_from.setDisplayFormat("HH:mm")
            t_to = QTimeEdit(QTime(17, 0))
            t_to.setDisplayFormat("HH:mm")
            self.hours_grid.addWidget(chk, d_idx, 0)
            self.hours_grid.addWidget(QLabel(tr("From")), d_idx, 1)
            self.hours_grid.addWidget(t_from, d_idx, 2)
            self.hours_grid.addWidget(QLabel(tr("To")), d_idx, 3)
            self.hours_grid.addWidget(t_to, d_idx, 4)
            self.day_rows.append((d_idx, chk, t_from, t_to))
        hv.addLayout(self.hours_grid)
        self.use_hours_chk.toggled.connect(self._sync_hours_enabled)
        v.addWidget(self.hours_box)

        # Per-surveyor overrides
        self.surv_box = QGroupBox(tr("Per-surveyor overrides (personal QR codes)"))
        sv = QVBoxLayout(self.surv_box)
        self.surv_table = QTableWidget(0, 2)
        self.surv_table.setHorizontalHeaderLabels([tr("Surveyor"), tr("Tracking mode")])
        self.surv_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.surv_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.surv_table.setMaximumHeight(140)
        sv.addWidget(self.surv_table)
        v.addWidget(self.surv_box)

        # Per-open-job overrides
        self.jobs_box = QGroupBox(tr("Per-open-job overrides"))
        jv = QVBoxLayout(self.jobs_box)
        self.jobs_table = QTableWidget(0, 2)
        self.jobs_table.setHorizontalHeaderLabels([tr("Capture job"), tr("Tracking mode")])
        self.jobs_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.jobs_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.jobs_table.setMaximumHeight(140)
        jv.addWidget(self.jobs_table)
        v.addWidget(self.jobs_box)

        self.status_lbl = QLabel()
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setObjectName("GpsHint")
        v.addWidget(self.status_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.cancel_btn = QPushButton(tr("Close"))
        self.save_btn = QPushButton(tr("Save policy"))
        self.save_btn.setObjectName("GpsPrimary")
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.save_btn)
        v.addLayout(btn_row)

        self.cancel_btn.clicked.connect(self.close)
        self.save_btn.clicked.connect(self.save_policy)

        self.load_current()

    def _cert_folder(self):
        return self.dock._cert_folder()

    def _photos_folder(self):
        return self.dock._active_folder()

    def _sync_hours_enabled(self, on):
        for _d, chk, t_from, t_to in self.day_rows:
            chk.setEnabled(bool(on))
            t_from.setEnabled(bool(on))
            t_to.setEnabled(bool(on))

    def _make_override_combo(self, current_val=""):
        cb = QComboBox()
        cb.addItem(tr("Use default"), "")
        for code, text in MODE_CHOICES:
            cb.addItem(tr(text), code)
        idx = cb.findData(current_val or "")
        cb.setCurrentIndex(max(0, idx))
        return cb

    def load_current(self):
        from .capture_jobs import JobStore
        from .server import read_surveyor_keys

        doc = policy.load(self._cert_folder())
        idx = self.mode_combo.findData(doc.get("mode") or "live")
        self.mode_combo.setCurrentIndex(max(0, idx))

        hrs = doc.get("hours") or []
        by_day = {}
        for r in hrs:
            if isinstance(r, (list, tuple)) and len(r) == 3 and r[0] not in by_day:
                by_day[int(r[0])] = (int(r[1]), int(r[2]))
        has_hrs = bool(by_day)
        self.use_hours_chk.setChecked(has_hrs)
        for d_idx, chk, t_from, t_to in self.day_rows:
            if d_idx in by_day:
                chk.setChecked(True)
                f_m, t_m = by_day[d_idx]
                t_from.setTime(_min_to_qtime(f_m))
                t_to.setTime(_min_to_qtime(min(1439, t_m)))
            else:
                chk.setChecked(d_idx in (0, 1, 2, 3, 4) and not has_hrs)
                t_from.setTime(QTime(8, 0))
                t_to.setTime(QTime(17, 0))
        self._sync_hours_enabled(has_hrs)

        per_map = doc.get("per") or {}
        skeys = read_surveyor_keys(self._photos_folder()).get("surveyors") or {}
        active_survs = [s for s in skeys.values() if isinstance(s, dict) and not s.get("revoked") and s.get("id")]
        active_survs.sort(key=lambda s: (str(s.get("name") or "").lower(), str(s.get("id") or "")))
        self.surv_table.setRowCount(len(active_survs))
        for i, s in enumerate(active_survs):
            sid = s["id"]
            sec = str(s.get("secret") or "")
            tag = policy.sk_tag(sec) if sec else ""
            cur_mode = per_map.get(sid) or (per_map.get(tag) if tag else "") or ""
            it = QTableWidgetItem(f"{s.get('name') or sid} ({sid})")
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            it.setData(Qt.ItemDataRole.UserRole, sid)
            self.surv_table.setItem(i, 0, it)
            self.surv_table.setCellWidget(i, 1, self._make_override_combo(cur_mode))

        jobs_map = doc.get("jobs") or {}
        open_jobs = JobStore(self._photos_folder()).list_jobs(status="open")
        self.jobs_table.setRowCount(len(open_jobs))
        for i, j in enumerate(open_jobs):
            jid = j["id"]
            cur_mode = jobs_map.get(jid) or ""
            it = QTableWidgetItem(f"{j.get('title') or jid} ({jid})")
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            it.setData(Qt.ItemDataRole.UserRole, jid)
            self.jobs_table.setItem(i, 0, it)
            self.jobs_table.setCellWidget(i, 1, self._make_override_combo(cur_mode))

        if doc.get("seq", 0) > 0:
            self.status_lbl.setText(tr("Active policy sequence: {seq}.").format(seq=doc["seq"]))
        else:
            self.status_lbl.setText(tr("No policy saved yet — phones use their own location toggle."))

    def collect_hours(self):
        if not self.use_hours_chk.isChecked():
            return []
        out = []
        for d_idx, chk, t_from, t_to in self.day_rows:
            if not chk.isChecked():
                continue
            f_min = _qtime_to_min(t_from.time(), is_end=False)
            t_min = _qtime_to_min(t_to.time(), is_end=True)
            if t_min <= f_min:
                t_min = min(1440, f_min + 60)
            out.append([d_idx, f_min, t_min])
        return out

    def collect_per_ids(self):
        out = {}
        for i in range(self.surv_table.rowCount()):
            it = self.surv_table.item(i, 0)
            cb = self.surv_table.cellWidget(i, 1)
            if it is None or cb is None:
                continue
            sid = it.data(Qt.ItemDataRole.UserRole)
            val = cb.currentData()
            if sid and val in policy.MODES:
                out[sid] = val
        return out

    def collect_jobs(self):
        out = {}
        for i in range(self.jobs_table.rowCount()):
            it = self.jobs_table.item(i, 0)
            cb = self.jobs_table.cellWidget(i, 1)
            if it is None or cb is None:
                continue
            jid = it.data(Qt.ItemDataRole.UserRole)
            val = cb.currentData()
            if jid and val in policy.MODES:
                out[jid] = val
        return out

    def save_policy(self):
        mode = self.mode_combo.currentData() or "live"
        hours = self.collect_hours()
        per_ids = self.collect_per_ids()
        jobs = self.collect_jobs()
        doc = policy.set_policy(
            self._cert_folder(),
            mode=mode,
            hours=hours,
            tz=policy.current_tz_minutes(),
            per_ids=per_ids,
            jobs=jobs,
        )
        if getattr(self.dock, "server", None) is not None:
            self.dock.server.refresh_policy()
        self.status_lbl.setText(tr("Active policy sequence: {seq}.").format(seq=doc["seq"]))
        if hasattr(self.dock, "_show_team"):
            self.dock._show_team()
        return doc


def open_tracking_dialog(dock):
    """Open or raise the TrackingDialog non-modally (`show()`)."""
    dlg = getattr(dock, "_tracking_dlg", None)
    if dlg is None:
        dlg = TrackingDialog(dock)
        dock._tracking_dlg = dlg
    else:
        dlg.load_current()
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg
