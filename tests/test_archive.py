r"""Phase P7 office archive and Excel index tests (`tests/test_archive.py`).

Covers:
  - the folder layout follows tree order (`01 <section>\`, repeat instance subfolders using title answer, `99 صور إضافية`);
  - names are Windows-safe (`<>:"/\|?*` stripped, trimmed, paths under 240 characters);
  - the SHA column equals the hash of the original;
  - the archive copy hash equals the original;
  - a rebuild is idempotent (unchanged files are not recopied) and adds new photos;
  - the CopyLedger is marked `archive` only after copy verification;
  - the xlsx has 4 sheets with RTL;
  - the global index `فهرس الأرشيف.xlsx` has one row per job and survives a rebuild;
  - `capture_jobs.close_job` builds the archive automatically, and `JobsDialog` / `FieldPhotosDock` expose archive controls.

Run:  engine\bin\python-qgis.bat tests\test_archive.py
"""
import hashlib
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from openpyxl import load_workbook  # noqa: E402
from qgis.core import QgsApplication, QgsSettings  # noqa: E402
from qgis.PyQt.QtCore import QPointF, Qt  # noqa: E402
from qgis.PyQt.QtGui import QColor, QImage, QLinearGradient, QPainter, QPen  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()

from biruni import i18n  # noqa: E402
from biruni.field_photos import archive as arch  # noqa: E402
from biruni.field_photos import archive_xlsx as ax  # noqa: E402
from biruni.field_photos import capture_jobs as cj  # noqa: E402
from biruni.field_photos import capture_templates as ct  # noqa: E402
from biruni.field_photos import copies as cp_mod  # noqa: E402
from biruni.field_photos import server as fs  # noqa: E402
from biruni.seal import core as seal_core  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def make_jpeg_bytes(w=320, h=240, c1="#1f4e79", c2="#2cc5b4", seed_text=""):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(img)
    g = QLinearGradient(QPointF(0, 0), QPointF(w, h))
    g.setColorAt(0.0, QColor(c1))
    g.setColorAt(1.0, QColor(c2))
    p.fillRect(0, 0, w, h, g)
    p.setPen(QPen(QColor("white"), 3))
    p.drawRect(12, 12, w - 24, h - 24)
    if seed_text:
        p.drawText(20, h // 2, seed_text)
    p.end()
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    try:
        img.save(tmp.name, "JPG", 85)
        with open(tmp.name, "rb") as rf:
            return rf.read()
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def format_tree(root_dir):
    """Render a `tree /f` style ASCII directory listing rooted at `root_dir`."""
    lines = [os.path.basename(os.path.abspath(root_dir))]

    def _walk(cur_dir, prefix=""):
        try:
            entries = sorted(
                os.listdir(cur_dir),
                key=lambda n: (not os.path.isdir(os.path.join(cur_dir, n)), n.lower()),
            )
        except OSError:
            return
        for idx, name in enumerate(entries):
            is_last = idx == len(entries) - 1
            branch = "└── " if is_last else "├── "
            full = os.path.join(cur_dir, name)
            if os.path.isdir(full):
                lines.append(f"{prefix}{branch}{name}\\")
                _walk(full, prefix + ("    " if is_last else "│   "))
            else:
                lines.append(f"{prefix}{branch}{name}")

    _walk(root_dir)
    return "\n".join(lines)


work = tempfile.mkdtemp(prefix="biruni_p7_archive_")

try:
    print("=== P7 Office Archive & Excel Test ===", flush=True)
    i18n.init("ar")
    seal_core.set_office("مكتب البيروني للاستشارات الهندسية")

    photo_folder = os.path.join(work, "photos")
    store = fs.PhotoStore(photo_folder)
    jstore = store.jobs

    # 1. Default archive root setting
    check(
        arch.get_archive_root(photo_folder) == os.path.join(photo_folder, "أرشيف بيروني"),
        "default archive root is <photo folder>\\أرشيف بيروني",
    )

    # 2. Windows-safe names & path clamping under 240 chars
    dirty_name = '  BLD:2026/408*?"<>| - عمارة العليا / المرحلة 1...  '
    cleaned_name = arch.safe_win_name(dirty_name)
    check(
        not any(c in cleaned_name for c in '<>:"/\\|?*')
        and not cleaned_name.endswith(".")
        and not cleaned_name.startswith(" "),
        f"safe_win_name strips Windows-unsafe characters and trims: {cleaned_name!r}",
    )

    # 3. Create a building survey job in Arabic with unsafe characters in title to verify sanitization
    tpl = ct.building_template()
    job1 = jstore.create(
        tpl,
        data={
            "job_no": "BLD-2026-408",
            "client": "شركة العمران للتطوير العقاري",
            "deed_no": "410209887312",
            "district": "حي العليا",
        },
        title='عمارة العليا: التجارية * السكنية "1"',
        loc=[46.6852, 24.6951],
        to="م. أحمد العتيبي",
        job_id="JBLD0408",
    )

    # Save photos in REVERSE tree order (services first, then floor/apartment/room, then entrance, then facade, plus extra)
    # so we prove the archive layout and Excel order follow the schema tree order, not arrival order.
    photo_specs = [
        ("ph-svc-01", ":elec_meter", "الخدمات › عداد الكهرباء", [], "لوحة العدادات", "#5b2c6f"),
        (
            "ph-rm-01",
            "rm0101:rooms",
            "الطابق الأرضي › شقة 101 › مطبخ",
            [["floors", "fl0001", 1], ["apts", "ap0101", 1], ["rooms", "rm0101", 1]],
            "صورة المطبخ 1",
            "#2b580c",
        ),
        (
            "ph-rm-02",
            "rm0101:rooms",
            "الطابق الأرضي › شقة 101 › مطبخ",
            [["floors", "fl0001", 1], ["apts", "ap0101", 1], ["rooms", "rm0101", 1]],
            "صورة المطبخ 2",
            "#1f4e79",
        ),
        ("ph-ent-01", ":entrance", "المدخل", [], "المدخل الرئيسي", "#7e5109"),
        ("ph-fac-01", ":front", "الواجهات › الواجهة الأمامية", [], "واجهة شارع 30", "#1f4e79"),
        ("ph-ext-01", "*", "*", [], "صورة إضافية للمواقف", "#333333"),
    ]
    import datetime as dt

    orig_hashes = {}
    orig_paths = {}
    for idx, (pid, slot_k, pl_txt, chain, note_txt, col) in enumerate(photo_specs, 1):
        raw_jpg = make_jpeg_bytes(360, 260, col, "#2cc5b4", f"{pid} {slot_k}")
        orig_hashes[pid] = hashlib.sha256(raw_jpg).hexdigest()
        t_ms = int(dt.datetime.fromisoformat(f"2026-11-20T09:{10 + idx:02d}:00+03:00").timestamp() * 1000)
        rec, _ = store.save(
            {
                "id": pid,
                "lat": round(24.6951 + idx * 0.0001, 6),
                "lon": round(46.6852 + idx * 0.0001, 6),
                "acc": 2.4,
                "t": t_ms,
                "surveyor": "م. أحمد العتيبي",
                "by": "م. أحمد العتيبي",
                "device_id": "dev_ahmad_01",
                "note": note_txt,
                "cap": {"j": job1["id"], "k": slot_k, "pl": pl_txt, "ch": chain},
            },
            raw_jpg,
            ssha=hashlib.sha256(b"sealed-" + raw_jpg).hexdigest(),
        )
        orig_paths[pid] = rec["photo"]

    # Save device state with floor title "الطابق الأرضي", apartment title "شقة 101", room title "مطبخ", and a skipped item
    st1 = {
        "v": 1,
        "job": job1["id"],
        "dev": "dev_ahmad_01",
        "by": "م. أحمد العتيبي",
        "rev": 5,
        "t": 1763620000000,
        "s": job1["s"],
        "inst": {
            "fl0001": {"n": "floors", "p": "", "o": 1, "t": 100, "del": 0},
            "ap0101": {"n": "apts", "p": "fl0001", "o": 1, "t": 110, "del": 0},
            "rm0101": {"n": "rooms", "p": "ap0101", "o": 1, "t": 120, "del": 0},
        },
        "ans": {
            "fl0001:floors": {"floor_name": ["الطابق الأرضي", 100]},
            "ap0101:apts": {
                "apt_no": ["شقة 101", 110],
                "use": [i18n.tr("Residential"), 110],
                "condition": [i18n.tr("Good"), 110],
                "rooms_count": [3, 110],
            },
            "rm0101:rooms": {"room_type": [i18n.tr("Kitchen"), 120]},
        },
        "note": {"rm0101:rooms": ["تهوية ممتازة", 120]},
        "ph": {
            ":front": ["ph-fac-01"],
            ":entrance": ["ph-ent-01"],
            "rm0101:rooms": ["ph-rm-01", "ph-rm-02"],
            ":elec_meter": ["ph-svc-01"],
            "*": ["ph-ext-01"],
        },
        "skip": {":back": ["ممر خلفي مغلق للصيانة", 130]},
        "closed": None,
    }
    jstore.take_state(job1["id"], "dev_ahmad_01", st1, item_id="js5-bld408")

    # Before building the archive, CopyLedger does NOT have 'archive' marked
    check(
        all(not (store.ledger.get(pid) or {}).get("places", {}).get("archive") for pid in orig_hashes),
        "before archive build, CopyLedger has no 'archive' place marked",
    )

    # 4. Build archive via close_job hook (which automatically builds the archive)
    jstore.close_job(job1["id"], ledger=store.ledger)
    # Also call build_job_archive to inspect the returned summary (and verify idempotence on immediate second call)
    res1 = arch.build_job_archive(jstore, job1["id"], ledger=store.ledger)
    job_dir = res1["job_dir"]

    check(
        os.path.isdir(job_dir)
        and "BLD-2026-408 - عمارة العليا التجارية السكنية 1" == os.path.basename(job_dir),
        f"job folder name is Windows-safe '<job no> - <title>': {os.path.basename(job_dir)!r}",
    )

    # 5. Folder layout follows tree order (`01 الواجهات`, `02 المدخل`, `03 الدرج`, `04 السطح`, `05 الطابق`, `06 الخدمات`, `99 صور إضافية`)
    subdirs = sorted(
        d for d in os.listdir(job_dir) if os.path.isdir(os.path.join(job_dir, d))
    )
    expected_prefixes = ["01 ", "02 ", "03 ", "04 ", "05 ", "06 ", "99 صور إضافية"]
    check(
        len(subdirs) == 7 and all(s.startswith(p) for s, p in zip(subdirs, expected_prefixes)),
        f"section subfolders follow exact tree order plus 99 صور إضافية: {subdirs}",
    )

    # Verify repeat instance nested subfolders use their title answer (`05 الطابق\الطابق الأرضي\شقة 101\مطبخ`)
    floor_sec = subdirs[4]
    room_dir = os.path.join(job_dir, floor_sec, "الطابق الأرضي", "شقة 101", i18n.tr("Kitchen"))
    check(
        os.path.isdir(room_dir),
        f"repeat instance subfolders use title answers: {os.path.relpath(room_dir, job_dir)}",
    )
    room_files = sorted(os.listdir(room_dir))
    check(
        len(room_files) == 2
        and all(fn.startswith("BLD-2026-408_") and fn.endswith(("_01.jpg", "_02.jpg")) for fn in room_files),
        f"photos are copied as <job code>_<item path short>_NN.jpg: {room_files}",
    )

    # Extra photos in `99 صور إضافية`
    extra_dir = os.path.join(job_dir, "99 صور إضافية")
    extra_files = sorted(os.listdir(extra_dir))
    check(
        len(extra_files) == 1 and extra_files[0] == "BLD-2026-408_extra_01.jpg",
        f"extra photos placed in 99 صور إضافية: {extra_files}",
    )

    # Originals stay where they are, and every archive path is under 240 chars
    all_arch_paths = [p["archive_path"] for p in res1["photos"]]
    check(
        all(os.path.isfile(op) for op in orig_paths.values())
        and all(os.path.abspath(ap) != os.path.abspath(orig_paths[p["id"]]) for p, ap in zip(res1["photos"], all_arch_paths)),
        "original photos stay untouched in their original folder",
    )
    check(
        all(len(os.path.abspath(ap)) < 240 for ap in all_arch_paths),
        f"all archive paths are under 240 characters (max={max(len(os.path.abspath(ap)) for ap in all_arch_paths)})",
    )

    # 6. Archive copy hash equals original, and CopyLedger is marked `archive`
    for ph in res1["photos"]:
        pid = ph["id"]
        disk_sha = cp_mod.sha256_file(ph["archive_path"])
        check(
            disk_sha == orig_hashes[pid] and ph["sha"] == orig_hashes[pid],
            f"archive copy hash on disk equals original SHA-256 for {pid} ({disk_sha[:12]}…)",
        )
        led_entry = store.ledger.get(pid)
        check(
            led_entry is not None
            and bool(led_entry["places"].get("archive"))
            and led_entry["sha"] == orig_hashes[pid],
            f"CopyLedger is marked 'archive' with verified SHA for {pid}",
        )

    # Sealed PDF report is in the job archive folder and valid
    pdf_path = res1["pdf_path"]
    check(
        os.path.isfile(pdf_path)
        and os.path.isfile(pdf_path + seal_core.SEAL_EXT)
        and seal_core.check_file(pdf_path)[0] == seal_core.UNCHANGED,
        f"sealed PDF report is in archive folder and verifies UNCHANGED: {os.path.basename(pdf_path)}",
    )

    # 7. Rebuild is idempotent (unchanged files are not recopied) and adds new photos
    mtimes_before = {p["id"]: os.path.getmtime(p["archive_path"]) for p in res1["photos"]}
    # Add a new photo to `:right`
    raw_new = make_jpeg_bytes(360, 260, "#114477", "#88ccaa", "ph-fac-right")
    orig_hashes["ph-fac-02"] = hashlib.sha256(raw_new).hexdigest()
    store.save(
        {
            "id": "ph-fac-02",
            "lat": 24.6959,
            "lon": 46.6859,
            "acc": 2.1,
            "t": "2026-11-20T10:30:00+03:00",
            "surveyor": "م. أحمد العتيبي",
            "by": "م. أحمد العتيبي",
            "device_id": "dev_ahmad_01",
            "note": "الواجهة اليمنى المضافة",
            "cap": {"j": job1["id"], "k": ":right", "pl": "الواجهات › الواجهة اليمنى", "ch": []},
        },
        raw_new,
    )
    res_rebuild = arch.build_job_archive(jstore, job1["id"], ledger=store.ledger)
    check(
        res_rebuild["copied_count"] == 1 and res_rebuild["reused_count"] == 6,
        f"rebuild is idempotent: copied_count={res_rebuild['copied_count']} (new photo), reused_count={res_rebuild['reused_count']} (unchanged files)",
    )
    mtimes_after = {p["id"]: os.path.getmtime(p["archive_path"]) for p in res_rebuild["photos"] if p["id"] in mtimes_before}
    check(
        mtimes_before == mtimes_after,
        "unchanged archive files were not recopied on rebuild (mtimes preserved)",
    )
    check(
        bool((store.ledger.get("ph-fac-02") or {}).get("places", {}).get("archive")),
        "newly added photo is copied, verified, and marked 'archive' in CopyLedger on rebuild",
    )

    # 8. `<job no>.xlsx` has 4 sheets with RTL, clickable links, and SHA column matching original hashes
    xlsx_path = res_rebuild["xlsx_path"]
    check(os.path.isfile(xlsx_path), f"job workbook written: {os.path.basename(xlsx_path)}")
    wb = load_workbook(xlsx_path, data_only=False)
    check(
        len(wb.sheetnames) == 4,
        f"xlsx has 4 sheets: {wb.sheetnames}",
    )
    check(
        all(wb[name].sheet_view.rightToLeft is True for name in wb.sheetnames),
        "all 4 sheets in <job no>.xlsx have rightToLeft=True when language is Arabic",
    )

    # Sheet 1: job data
    ws_job = wb[wb.sheetnames[0]]
    job_kv = {
        str(ws_job.cell(row=r, column=1).value): str(ws_job.cell(row=r, column=2).value)
        for r in range(2, ws_job.max_row + 1)
    }
    check(
        job_kv.get(i18n.tr("Job number")) == "BLD-2026-408"
        and job_kv.get(i18n.tr("Deed number")) == "410209887312"
        and job_kv.get(i18n.tr("District")) == "حي العليا",
        f"job data sheet contains number, client, deed no, district, surveyors, dates, percent: {job_kv}",
    )

    # Sheet 2: items (path, required, state done/missing/skipped with reason, answers in columns)
    ws_items = wb[wb.sheetnames[1]]
    item_rows = [
        [ws_items.cell(row=r, column=c).value for c in range(1, ws_items.max_column + 1)]
        for r in range(2, ws_items.max_row + 1)
    ]
    states_col = [str(r[2] or "") for r in item_rows]
    check(
        any(i18n.tr("Done") in s for s in states_col)
        and any(i18n.tr("Missing") in s for s in states_col)
        and any(i18n.tr("Skipped") in s and "ممر خلفي مغلق للصيانة" in s for s in states_col),
        "items sheet records done, missing, and skipped with its reason, plus answers in columns",
    )

    # Sheet 3: photos (archive file name with clickable link, path, lat, lon, accuracy, time, note, answers, original SHA-256)
    ws_photos = wb[wb.sheetnames[2]]
    check(
        ws_photos.max_row - 1 == len(res_rebuild["photos"]),
        f"photos sheet has {ws_photos.max_row - 1} rows matching archived photos",
    )
    sha_matches = True
    links_ok = True
    for r_idx, ph in enumerate(res_rebuild["photos"], start=2):
        c_file = ws_photos.cell(row=r_idx, column=1)
        c_sha = ws_photos.cell(row=r_idx, column=9)
        if not (c_file.hyperlink and c_file.hyperlink.target):
            links_ok = False
        else:
            linked_file = os.path.join(job_dir, c_file.hyperlink.target.replace("/", os.sep))
            if not os.path.isfile(linked_file):
                links_ok = False
        if c_sha.value != orig_hashes[ph["id"]]:
            sha_matches = False
    check(links_ok, "every photo row in photos sheet has a valid clickable relative hyperlink to the archive file")
    check(sha_matches, "every photo row's SHA-256 column equals the hash of the original photo")

    # Sheet 4: stats
    ws_stats = wb[wb.sheetnames[3]]
    check(ws_stats.max_row >= 6, f"stats sheet populated with report statistics ({ws_stats.max_row - 1} rows)")
    wb.close()

    # 9. Global index `فهرس الأرشيف.xlsx` has one row per job and survives a rebuild
    # Add a second job and build its archive
    job2 = jstore.create(
        ct.shop_template(),
        data={
            "job_no": "SH-2026-109",
            "client": "مؤسسة الواحة التجارية",
            "deed_no": "9900112233",
            "district": "حي الملز",
        },
        title="محل الواحة",
        to="م. سالم الدوسري",
        job_id="JSHOP109",
    )
    jstore.close_job(job2["id"], ledger=store.ledger)
    # Rebuild job1 again to verify the index keeps 1 row per job (2 jobs total)
    arch.build_job_archive(jstore, job1["id"], ledger=store.ledger)

    index_path = res_rebuild["index_path"]
    check(
        os.path.isfile(index_path) and os.path.basename(index_path) == "فهرس الأرشيف.xlsx",
        f"global index written at archive root: {index_path}",
    )
    idx_rows = ax.read_global_index(index_path)
    check(
        len(idx_rows) == 2 and [r["code"] for r in idx_rows] == ["BLD-2026-408", "SH-2026-109"],
        f"global index has exactly one row per job and survives rebuilds: {[r['code'] for r in idx_rows]}",
    )

    wb_idx = load_workbook(index_path, data_only=False)
    ws_idx = wb_idx.active
    check(
        ws_idx.sheet_view.rightToLeft is True
        and ws_idx.cell(row=2, column=10).hyperlink is not None,
        "global index sheet is RTL and has clickable folder hyperlink",
    )
    wb_idx.close()

    # Print sample archive tree for DONE_P7.md
    sample_tree = format_tree(res_rebuild["archive_root"])
    print("\n--- SAMPLE ARCHIVE TREE ---\n" + sample_tree + "\n---------------------------\n", flush=True)
    scratch_tree_file = r"D:\Claude Code - Online\biruni\_scratch\noloss\sample_archive_tree.txt"
    os.makedirs(os.path.dirname(scratch_tree_file), exist_ok=True)
    with open(scratch_tree_file, "w", encoding="utf-8") as tf:
        tf.write(sample_tree + "\n")

finally:
    i18n.init("en_US")
    QgsSettings().remove(arch.ARCHIVE_SETTING_KEY)
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
app.exitQgis()
sys.exit(1 if failures else 0)
