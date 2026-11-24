"""Search the office archive index (`فهرس الأرشيف.xlsx`) and open or map-zoom matching jobs (Phase P7)."""
import datetime as dt
import os
import re

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsRectangle,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import QDate, Qt, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QDateEdit, QDialog, QDialogButtonBox, QFormLayout,
                                 QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTreeWidget,
                                 QTreeWidgetItem, QVBoxLayout)

from ..i18n import tr
from .archive import INDEX_FILENAME, get_archive_root
from .archive_xlsx import read_global_index

_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _extract_date_bounds(dates_str):
    """Extract `(min_date, max_date)` as `datetime.date` objects from a dates cell string."""
    found = []
    for m in _DATE_RE.findall(str(dates_str or "")):
        try:
            found.append(dt.date.fromisoformat(m))
        except ValueError:
            continue
    if not found:
        return None, None
    return min(found), max(found)


def _to_date(val):
    if not val:
        return None
    if isinstance(val, dt.datetime):
        return val.date()
    if isinstance(val, dt.date):
        return val
    if isinstance(val, QDate):
        return dt.date(val.year(), val.month(), val.day())
    s = str(val).strip()[:10]
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def resolve_entry_folder(archive_root, entry):
    """Resolve an index entry's relative or absolute folder path against `archive_root`."""
    raw = str((entry or {}).get("folder") or "").strip()
    if not raw:
        return ""
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(os.path.abspath(archive_root), raw))


def search_archive(
    archive_root,
    client="",
    deed_no="",
    district="",
    date_from=None,
    date_to=None,
    surveyor="",
    query="",
):
    """Search `فهرس الأرشيف.xlsx` at `archive_root` by client, deed_no, district, date range, or surveyor.

    Returns a list of matching index row dicts, each enriched with `folder_path` (absolute path to the job folder).
    """
    index_path = (
        archive_root
        if str(archive_root).lower().endswith(".xlsx")
        else os.path.join(str(archive_root or ""), INDEX_FILENAME)
    )
    root_dir = os.path.dirname(os.path.abspath(index_path))
    rows = read_global_index(index_path)

    q_client = str(client or "").strip().lower()
    q_deed = str(deed_no or "").strip().lower()
    q_dist = str(district or "").strip().lower()
    q_surv = str(surveyor or "").strip().lower()
    q_any = str(query or "").strip().lower()
    d_from = _to_date(date_from)
    d_to = _to_date(date_to)
    if d_from and d_to and d_from > d_to:
        d_from, d_to = d_to, d_from

    out = []
    for r in rows:
        if q_client and q_client not in (r.get("client") or "").lower():
            continue
        if q_deed and q_deed not in (r.get("deed_no") or "").lower():
            continue
        if q_dist and q_dist not in (r.get("district") or "").lower():
            continue
        if q_surv and q_surv not in (r.get("surveyors") or "").lower():
            continue
        if q_any:
            hay = " ".join(
                str(r.get(k) or "")
                for k in ("code", "title", "client", "deed_no", "district", "surveyors", "job_id")
            ).lower()
            if q_any not in hay:
                continue
        if d_from is not None or d_to is not None:
            r_min, r_max = _extract_date_bounds(r.get("dates") or "")
            if r_min is None or r_max is None:
                continue
            if d_from is not None and r_max < d_from:
                continue
            if d_to is not None and r_min > d_to:
                continue

        enriched = dict(r)
        enriched["folder_path"] = resolve_entry_folder(root_dir, r)
        out.append(enriched)
    return out


def open_job_folder(entry_or_path, archive_root="", opener=None):
    """Open the job's archive folder on disk (returns the resolved folder path, or '' if missing)."""
    if isinstance(entry_or_path, dict):
        path = entry_or_path.get("folder_path") or resolve_entry_folder(archive_root, entry_or_path)
    else:
        path = str(entry_or_path or "").strip()
        if path and not os.path.isabs(path) and archive_root:
            path = os.path.normpath(os.path.join(os.path.abspath(archive_root), path))
    if not path or not os.path.isdir(path):
        return ""
    if opener is not None:
        opener(path)
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
    return path


def zoom_map_to_job(job_id, store=None, records=(), iface=None, layer=None, folder_path=""):
    """Select and zoom the map canvas to a job's photos (from the photos layer, records, or `<job no>.xlsx`)."""
    jid = str(job_id or "").strip()
    pts = []
    fids = []

    lyr = layer
    if lyr is None and store is not None:
        gpkg_norm = os.path.normcase(os.path.join(store.folder, "field_photos.gpkg"))
        for cand in QgsProject.instance().mapLayers().values():
            if isinstance(cand, QgsVectorLayer) and cand.isValid():
                src = os.path.normcase(cand.source().split("|")[0])
                if src == gpkg_norm or cand.fields().indexOf("job_id") >= 0:
                    lyr = cand
                    break

    if lyr is not None and isinstance(lyr, QgsVectorLayer) and lyr.isValid():
        j_idx = lyr.fields().indexOf("job_id")
        if j_idx >= 0 and jid:
            for f in lyr.getFeatures():
                if f["job_id"] == jid:
                    fids.append(f.id())
                    if not f.geometry().isEmpty():
                        pt = f.geometry().asPoint()
                        pts.append((pt.x(), pt.y()))
            if fids:
                lyr.selectByIds(fids)

    if not pts and records and jid:
        for r in records:
            if not isinstance(r, dict):
                continue
            cap = r.get("cap")
            if isinstance(cap, dict) and cap.get("j") == jid:
                lat, lon = r.get("lat"), r.get("lon")
                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                    pts.append((float(lon), float(lat)))

    if not pts and store is not None and jid:
        job = store.get(jid)
        if isinstance(job, dict) and isinstance(job.get("loc"), (list, tuple)) and len(job["loc"]) == 2:
            try:
                pts.append((float(job["loc"][0]), float(job["loc"][1])))
            except (TypeError, ValueError):
                pass

    if not pts and folder_path and os.path.isdir(folder_path):
        from openpyxl import load_workbook

        for fn in os.listdir(folder_path):
            if fn.lower().endswith(".xlsx"):
                try:
                    wb = load_workbook(os.path.join(folder_path, fn), data_only=True)
                    if len(wb.sheetnames) >= 3:
                        ws = wb[wb.sheetnames[2]]
                        for row_idx in range(2, ws.max_row + 1):
                            lat = ws.cell(row=row_idx, column=3).value
                            lon = ws.cell(row=row_idx, column=4).value
                            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                                pts.append((float(lon), float(lat)))
                    wb.close()
                except Exception:  # noqa: BLE001
                    pass
                break

    if not pts:
        return None

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    rect = QgsRectangle(min(xs), min(ys), max(xs), max(ys))
    if rect.width() < 0.0005 or rect.height() < 0.0005:
        rect.grow(0.001)
    else:
        rect.scale(1.25)

    if iface is not None and hasattr(iface, "mapCanvas") and iface.mapCanvas() is not None:
        canvas = iface.mapCanvas()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        dest_crs = canvas.mapSettings().destinationCrs()
        if dest_crs.isValid() and dest_crs != wgs84:
            try:
                rect = QgsCoordinateTransform(wgs84, dest_crs, QgsProject.instance()).transformBoundingBox(rect)
            except Exception:  # noqa: BLE001
                pass
        canvas.setExtent(rect)
        canvas.refresh()
    return rect


class ArchiveSearchDialog(QDialog):
    """Dialog to search `فهرس الأرشيف.xlsx` by client, deed no, district, date range or surveyor."""

    def __init__(self, store=None, archive_root=None, records=(), iface=None, opener=None, parent=None):
        super().__init__(parent)
        self.store = store
        self.archive_root = archive_root or get_archive_root(store.folder if store is not None else "")
        self.records = list(records or ())
        self.iface = iface
        self.opener = opener
        self.last_opened_folder = ""
        self.progress_dlg = None

        self.setWindowTitle(tr("Search archive"))
        self.resize(860, 500)

        v = QVBoxLayout(self)
        form = QFormLayout()

        row1 = QHBoxLayout()
        self.client_edit = QLineEdit()
        self.client_edit.setPlaceholderText(tr("Client / owner"))
        self.client_edit.textChanged.connect(self.run_search)
        self.deed_edit = QLineEdit()
        self.deed_edit.setPlaceholderText(tr("Deed number"))
        self.deed_edit.textChanged.connect(self.run_search)
        row1.addWidget(QLabel(tr("Client / owner") + ":"))
        row1.addWidget(self.client_edit, 1)
        row1.addWidget(QLabel(tr("Deed number") + ":"))
        row1.addWidget(self.deed_edit, 1)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        self.district_edit = QLineEdit()
        self.district_edit.setPlaceholderText(tr("District"))
        self.district_edit.textChanged.connect(self.run_search)
        self.surveyor_edit = QLineEdit()
        self.surveyor_edit.setPlaceholderText(tr("Surveyor"))
        self.surveyor_edit.textChanged.connect(self.run_search)
        row2.addWidget(QLabel(tr("District") + ":"))
        row2.addWidget(self.district_edit, 1)
        row2.addWidget(QLabel(tr("Surveyor") + ":"))
        row2.addWidget(self.surveyor_edit, 1)
        v.addLayout(row2)

        row3 = QHBoxLayout()
        self.date_box = QCheckBox(tr("Filter by date range:"))
        self.date_box.toggled.connect(self.run_search)
        self.from_date = QDateEdit(QDate.currentDate().addMonths(-1))
        self.from_date.setDisplayFormat("yyyy-MM-dd")
        self.from_date.setCalendarPopup(True)
        self.from_date.dateChanged.connect(self.run_search)
        self.to_date = QDateEdit(QDate.currentDate())
        self.to_date.setDisplayFormat("yyyy-MM-dd")
        self.to_date.setCalendarPopup(True)
        self.to_date.dateChanged.connect(self.run_search)
        self.clear_btn = QPushButton(tr("Clear"))
        self.clear_btn.clicked.connect(self.clear_filters)
        row3.addWidget(self.date_box)
        row3.addWidget(self.from_date)
        row3.addWidget(QLabel("→"))
        row3.addWidget(self.to_date)
        row3.addStretch(1)
        row3.addWidget(self.clear_btn)
        v.addLayout(row3)
        v.addLayout(form)

        self.table = QTreeWidget()
        self.table.setRootIsDecorated(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setHeaderLabels([
            tr("Job number"),
            tr("Job title:"),
            tr("Client / owner"),
            tr("Deed number"),
            tr("District"),
            tr("Dates"),
            tr("Surveyor"),
            tr("Completion"),
            tr("Status"),
        ])
        self.table.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemDoubleClicked.connect(lambda *_: self.open_selected_folder())
        v.addWidget(self.table, 1)

        brow = QHBoxLayout()
        self.open_folder_btn = QPushButton(tr("Open folder"))
        self.open_folder_btn.clicked.connect(self.open_selected_folder)
        self.progress_btn = QPushButton(tr("Progress…"))
        self.progress_btn.clicked.connect(self.open_selected_progress)
        self.zoom_btn = QPushButton(tr("Zoom on map"))
        self.zoom_btn.clicked.connect(self.zoom_selected_on_map)
        for b in (self.open_folder_btn, self.progress_btn, self.zoom_btn):
            brow.addWidget(b)
        brow.addStretch(1)
        self.status_lbl = QLabel()
        brow.addWidget(self.status_lbl)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        brow.addWidget(buttons)
        v.addLayout(brow)

        self.results = []
        self.run_search()

    def clear_filters(self):
        for w in (self.client_edit, self.deed_edit, self.district_edit, self.surveyor_edit):
            w.blockSignals(True)
            w.clear()
            w.blockSignals(False)
        self.date_box.blockSignals(True)
        self.date_box.setChecked(False)
        self.date_box.blockSignals(False)
        self.run_search()

    def run_search(self, *_a):
        d_from = self.from_date.date() if self.date_box.isChecked() else None
        d_to = self.to_date.date() if self.date_box.isChecked() else None
        self.results = search_archive(
            self.archive_root,
            client=self.client_edit.text(),
            deed_no=self.deed_edit.text(),
            district=self.district_edit.text(),
            date_from=d_from,
            date_to=d_to,
            surveyor=self.surveyor_edit.text(),
        )
        self.table.clear()
        for r in self.results:
            it = QTreeWidgetItem([
                r.get("code") or "",
                r.get("title") or "",
                r.get("client") or "",
                r.get("deed_no") or "",
                r.get("district") or "",
                r.get("dates") or "",
                r.get("surveyors") or "",
                str(r.get("percent") or ""),
                r.get("status") or "",
            ])
            it.setData(0, Qt.ItemDataRole.UserRole, r)
            self.table.addTopLevelItem(it)
        if self.table.topLevelItemCount() > 0:
            self.table.setCurrentItem(self.table.topLevelItem(0))
        self.status_lbl.setText(tr("{n} jobs found.").format(n=len(self.results)))
        return self.results

    def selected_entry(self):
        it = self.table.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it is not None else None

    def open_selected_folder(self):
        entry = self.selected_entry()
        if not entry:
            return ""
        path = open_job_folder(entry, archive_root=self.archive_root, opener=self.opener)
        self.last_opened_folder = path
        return path

    def open_selected_progress(self, modal=True):
        entry = self.selected_entry()
        if not entry or self.store is None:
            return None
        from .capture_progress import ProgressDialog

        jid = entry.get("job_id") or entry.get("code") or ""
        if not jid:
            return None
        dlg = ProgressDialog(self.store, jid, self.records, self.iface, parent=self)
        self.progress_dlg = dlg
        if modal:
            dlg.exec()
            self.progress_dlg = None
        return dlg

    def zoom_selected_on_map(self):
        entry = self.selected_entry()
        if not entry:
            return None
        jid = entry.get("job_id") or entry.get("code") or ""
        return zoom_map_to_job(
            jid,
            store=self.store,
            records=self.records,
            iface=self.iface,
            folder_path=entry.get("folder_path") or "",
        )
