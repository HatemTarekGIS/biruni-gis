r"""Phase P7 office archive search and dialog tests (`tests/test_archive_search.py`).

Covers:
  - search by deed number, district, client, surveyor, and date range;
  - the results open the right job archive folder;
  - opening the progress view and zooming the map to the job's photos from the search dialog;
  - `JobsDialog` "Build archive" and "Search archive…" buttons.

Run:  engine\bin\python-qgis.bat tests\test_archive_search.py
"""
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtCore import QDate, QPointF  # noqa: E402
from qgis.PyQt.QtGui import QColor, QImage, QLinearGradient, QPainter  # noqa: E402
from qgis.PyQt.QtWidgets import QMainWindow  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()

from biruni import i18n  # noqa: E402
from biruni.field_photos import archive as arch  # noqa: E402
from biruni.field_photos import archive_search as asearch  # noqa: E402
from biruni.field_photos import capture_jobs as cj  # noqa: E402
from biruni.field_photos import capture_templates as ct  # noqa: E402
from biruni.field_photos import dock as fd  # noqa: E402
from biruni.field_photos import server as fs  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def make_jpeg_bytes(c1="#1f4e79", c2="#2cc5b4"):
    img = QImage(200, 150, QImage.Format.Format_RGB32)
    p = QPainter(img)
    g = QLinearGradient(QPointF(0, 0), QPointF(200, 150))
    g.setColorAt(0.0, QColor(c1))
    g.setColorAt(1.0, QColor(c2))
    p.fillRect(0, 0, 200, 150, g)
    p.end()
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    try:
        img.save(tmp.name, "JPG", 82)
        with open(tmp.name, "rb") as rf:
            return rf.read()
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


class DummyIface:
    def __init__(self):
        self.mw = QMainWindow()
        self.canvas = QgsMapCanvas(self.mw)
        self.mw.setCentralWidget(self.canvas)

    def mainWindow(self):
        return self.mw

    def mapCanvas(self):
        return self.canvas


work = tempfile.mkdtemp(prefix="biruni_p7_search_")

try:
    print("=== P7 Archive Search Test ===", flush=True)
    i18n.init("ar")
    photo_folder = os.path.join(work, "photos")
    store = fs.PhotoStore(photo_folder)
    jstore = store.jobs
    jpg = make_jpeg_bytes()

    # Create 3 distinct jobs with different deed numbers, districts, dates, and surveyors
    jobs_spec = [
        (
            "JJOB0001",
            "VL-2026-101",
            "أرض النرجس السكنية",
            "شركة الشمال العقارية",
            "310055443322",
            "حي النرجس",
            "م. خالد الحربي",
            "2026-10-05T09:15:00+03:00",
            24.8120,
            46.6410,
        ),
        (
            "JJOB0002",
            "VL-2026-102",
            "فيلا الياسمين",
            "مؤسسة البيان",
            "780099887766",
            "حي الياسمين",
            "م. فهد القحطاني",
            "2026-11-12T11:20:00+03:00",
            24.8350,
            46.6520,
        ),
        (
            "JJOB0003",
            "VL-2026-103",
            "محل الملقا التجاري",
            "شركة الشمال العقارية",
            "550011229988",
            "حي الملقا",
            "م. خالد الحربي",
            "2026-11-22T14:45:00+03:00",
            24.7910,
            46.6010,
        ),
    ]

    import datetime as dt

    job_dirs = {}
    tpl = ct.shop_template()
    for jid, jno, title, client, deed, dist, surv, taken_iso, lat, lon in jobs_spec:
        t_ms = int(dt.datetime.fromisoformat(taken_iso).timestamp() * 1000)
        j = jstore.create(
            tpl,
            data={"job_no": jno, "client": client, "deed_no": deed, "district": dist},
            title=title,
            loc=[lon, lat],
            to=surv,
            job_id=jid,
        )
        store.save(
            {
                "id": f"ph-{jid.lower()}",
                "lat": lat,
                "lon": lon,
                "acc": 2.0,
                "t": t_ms,
                "surveyor": surv,
                "by": surv,
                "device_id": f"dev_{jid.lower()}",
                "note": title,
                "cap": {"j": j["id"], "k": ":signboard", "pl": "الواجهة", "ch": []},
            },
            jpg,
        )
        res = arch.build_job_archive(jstore, j["id"], ledger=store.ledger)
        job_dirs[jno] = res["job_dir"]

    archive_root = arch.get_archive_root(photo_folder)
    recs = fs.read_log(photo_folder)
    lyr = fd.open_layer(photo_folder, add=True)
    fd.add_records(lyr, recs)
    iface = DummyIface()

    # 1. Search by deed number
    by_deed = asearch.search_archive(archive_root, deed_no="780099887766")
    check(
        len(by_deed) == 1 and by_deed[0]["code"] == "VL-2026-102",
        f"search by deed number finds exact job: {[r['code'] for r in by_deed]}",
    )

    # 2. Search by district
    by_dist = asearch.search_archive(archive_root, district="النرجس")
    check(
        len(by_dist) == 1 and by_dist[0]["code"] == "VL-2026-101",
        f"search by district finds matching job: {[r['code'] for r in by_dist]}",
    )

    # 3. Search by date range
    by_dates = asearch.search_archive(archive_root, date_from="2026-11-01", date_to="2026-11-15")
    check(
        len(by_dates) == 1 and by_dates[0]["code"] == "VL-2026-102",
        f"search by date range [2026-11-01..2026-11-15] finds VL-2026-102: {[r['code'] for r in by_dates]}",
    )
    by_nov = asearch.search_archive(archive_root, date_from="2026-11-01", date_to="2026-11-30")
    check(
        [r["code"] for r in by_nov] == ["VL-2026-102", "VL-2026-103"],
        f"search by November date range finds both November jobs: {[r['code'] for r in by_nov]}",
    )

    # 4. Search by client & surveyor
    by_client_surv = asearch.search_archive(archive_root, client="الشمال", surveyor="خالد")
    check(
        [r["code"] for r in by_client_surv] == ["VL-2026-101", "VL-2026-103"],
        f"search by client + surveyor finds matching jobs: {[r['code'] for r in by_client_surv]}",
    )

    # 5. Dialog interaction: filter, open folder, open progress view, zoom map
    opened_folders = []
    dlg = asearch.ArchiveSearchDialog(
        store=jstore,
        archive_root=archive_root,
        records=recs,
        iface=iface,
        opener=lambda p: opened_folders.append(p),
    )
    check(dlg.table.topLevelItemCount() == 3, "ArchiveSearchDialog starts with all 3 indexed jobs")

    # Filter by deed number in dialog and open folder
    dlg.deed_edit.setText("550011229988")
    check(
        dlg.table.topLevelItemCount() == 1 and dlg.selected_entry()["code"] == "VL-2026-103",
        "typing deed number in ArchiveSearchDialog filters table live to VL-2026-103",
    )
    opened = dlg.open_selected_folder()
    check(
        os.path.normcase(opened) == os.path.normcase(job_dirs["VL-2026-103"])
        and len(opened_folders) == 1
        and os.path.isdir(opened),
        f"open_selected_folder opens the exact archive folder for VL-2026-103: {opened}",
    )

    # Filter by date range in dialog and open folder
    dlg.clear_filters()
    dlg.date_box.setChecked(True)
    dlg.from_date.setDate(QDate(2026, 10, 1))
    dlg.to_date.setDate(QDate(2026, 10, 31))
    check(
        dlg.table.topLevelItemCount() == 1 and dlg.selected_entry()["code"] == "VL-2026-101",
        "date range filter in dialog selects VL-2026-101",
    )
    opened_oct = dlg.open_selected_folder()
    check(
        os.path.normcase(opened_oct) == os.path.normcase(job_dirs["VL-2026-101"]),
        f"open_selected_folder opens VL-2026-101 folder: {opened_oct}",
    )

    # Open progress dialog and zoom map to the selected job's photos
    pdlg = dlg.open_selected_progress(modal=False)
    check(
        pdlg is not None and pdlg.job_id == "JJOB0001",
        f"open_selected_progress opens ProgressDialog for {pdlg.job_id if pdlg else None}",
    )
    if pdlg is not None:
        pdlg.close()

    rect = dlg.zoom_selected_on_map()
    sel_pids = [f["photo_id"] for f in lyr.getSelectedFeatures()]
    check(
        rect is not None and sel_pids == ["ph-jjob0001"],
        f"zoom_selected_on_map selects job photos on layer and zooms map extent ({sel_pids})",
    )

    # 6. JobsDialog 'Build archive' and 'Search archive…' buttons
    jdlg = cj.JobsDialog(jstore, records=recs, iface=iface)
    built_res = jdlg._build_archive_selected()
    check(
        built_res is not None and os.path.isdir(built_res["job_dir"]),
        "JobsDialog 'Build archive' button builds the selected job's archive",
    )
    sdlg = jdlg._open_archive_search(modal=False)
    check(
        sdlg is not None and sdlg.table.topLevelItemCount() == 3,
        "JobsDialog 'Search archive…' button opens ArchiveSearchDialog",
    )
    if sdlg is not None:
        sdlg.close()

finally:
    i18n.init("en_US")
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
app.exitQgis()
sys.exit(1 if failures else 0)
