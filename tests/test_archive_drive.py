r"""Phase P8 Google Drive archive tests (`tests/test_archive_drive.py`).

Covers: AES-256 (AE-2) ZIP opens with the password (own reader and 7-Zip when installed) and fails with a wrong one;
EXIF survives every compression level (Pillow and the Qt fallback) and the savings are measured; a simulated failed
(truncated) write is never marked; the 10%-free warning; a rebuild is idempotent; the formats and the close_job hook.

Run:  engine\bin\python-qgis.bat tests\test_archive_drive.py
"""
import hashlib
import io
import os
import random
import shutil
import subprocess
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import QgsApplication  # noqa: E402

SETTINGS_DIR = tempfile.mkdtemp()
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR
app = QgsApplication([], True)
app.initQgis()

from PIL import Image  # noqa: E402

from biruni import i18n  # noqa: E402
from biruni.field_photos import archive as arch  # noqa: E402
from biruni.field_photos import archive_drive as ad  # noqa: E402
from biruni.field_photos import capture_templates as ct  # noqa: E402
from biruni.field_photos import server as fs  # noqa: E402
from biruni.seal import core as seal_core  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def exif_jpeg(w, h, seed):
    """A noisy-but-smooth photo-like JPEG with EXIF (Make, DateTime, GPS)."""
    rnd = random.Random(seed)
    base = Image.effect_noise((w // 8, h // 8), 60).convert("RGB").resize((w, h), Image.BICUBIC)
    px = Image.effect_noise((w, h), 14).convert("RGB")
    im = Image.blend(base, px, 0.35)
    ex = Image.Exif()
    ex[0x010F] = "BiruniCam"
    ex[0x0110] = f"T-{seed}"
    ex[0x0132] = "2026:11:20 09:15:00"
    ex[0x8825] = {1: "N", 2: (24.0, 41.0, 42.0 + rnd.random()), 3: "E", 4: (46.0, 41.0, 6.0)}
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95, exif=ex)
    return buf.getvalue()


def exif_of(data):
    ex = Image.open(io.BytesIO(data)).getexif()
    return ex.get(0x010F), ex.get(0x0110), ex.get(0x0132), dict(ex.get_ifd(0x8825)).get(1)


work = tempfile.mkdtemp(prefix="biruni_p8_")
try:
    print("=== P8 Google Drive archive test ===", flush=True)
    i18n.init("ar")
    seal_core.set_office("مكتب البيروني")

    # ---- 1. AES ZIP ---------------------------------------------------------------------------------------------
    pw = ad.generate_password()
    check(len(pw) >= 30 and pw != ad.generate_password(), f"generated password is long and random: {pw}")
    payload = {"A-1.jpg": os.urandom(300001), "A-1.xlsx": b"x" * 5000, "ملف.txt": "عربي".encode()}
    zp = os.path.join(work, "t.zip")
    ad.write_aes_zip(zp, list(payload.items()), pw)
    check(ad.read_aes_zip(zp, pw) == payload, "AES ZIP round-trips with the right password (own reader)")
    try:
        ad.read_aes_zip(zp, pw + "x")
        wrong = False
    except ad.WrongPassword:
        wrong = True
    check(wrong, "AES ZIP refuses a wrong password (own reader)")
    z7 = r"C:\Program Files\7-Zip\7z.exe"
    if os.path.isfile(z7):
        r = subprocess.run([z7, "t", "-p" + pw, zp], capture_output=True, text=True)
        check(r.returncode == 0 and "Everything is Ok" in r.stdout, "7z t -p<password> passes")
        r = subprocess.run([z7, "t", "-pWRONG", zp], capture_output=True, text=True)
        check(r.returncode != 0, "7z t with a wrong password fails")
        out = os.path.join(work, "x7")
        r = subprocess.run([z7, "x", "-p" + pw, "-o" + out, "-y", zp], capture_output=True, text=True)
        with open(os.path.join(out, "A-1.jpg"), "rb") as f:
            check(f.read() == payload["A-1.jpg"], "7z extracts byte-identical content")
    else:
        print("SKIP 7-Zip not installed", flush=True)

    # ---- 2. password storage ---------------------------------------------------------------------------------------
    cf = os.path.join(work, "cert")
    p1, created = ad.ensure_password(cf)
    p2, created2 = ad.ensure_password(cf)
    with open(os.path.join(cf, ad.PASSWORD_FILE), encoding="ascii") as f:
        stored = f.read()
    check(created and not created2 and p1 == p2 and p1 not in stored, "password created once, stored protected (not plain)")

    # ---- 3. compression levels, EXIF, savings ----------------------------------------------------------------------
    big = exif_jpeg(5200, 3900, 1)
    base_exif = exif_of(big)
    for forced_pil in (True, False):
        ad._HAVE_PIL = forced_pil
        for lvl in ("original", "high", "compact"):
            out = ad.compress_jpeg(big, lvl)
            im = Image.open(io.BytesIO(out))
            ok = exif_of(out) == base_exif
            note = f"{'Pillow' if forced_pil else 'Qt'} {lvl}: {len(big) / 1e6:.2f} MB -> {len(out) / 1e6:.2f} MB " \
                   f"({100 - 100 * len(out) / len(big):.0f}% saved), {im.size[0]}x{im.size[1]}"
            check(ok, "EXIF survives, " + note)
            if lvl == "high":
                check(max(im.size) <= 4000 and len(out) < len(big), "high is capped at 4000 px and smaller")
            if lvl == "compact":
                check(max(im.size) <= 2560 and len(out) < len(big), "compact is capped at 2560 px and smaller")
    ad._HAVE_PIL = True

    # ---- 4. a job archive to build from ----------------------------------------------------------------------------
    photo_folder = os.path.join(work, "photos")
    store = fs.PhotoStore(photo_folder)
    jstore = store.jobs
    job = jstore.create(ct.building_template(), data={"job_no": "BLD-2026-777", "client": "عميل", "deed_no": "1"},
                        title="مبنى الاختبار", loc=[46.68, 24.69], to="م. أحمد", job_id="JP8TEST")
    for idx, (pid, k) in enumerate((("p8-photo-a", ":front"), ("p8-photo-b", ":entrance"), ("p8-photo-c", "*")), 1):
        raw = exif_jpeg(2400, 1800, idx)
        store.save({"id": pid, "lat": 24.69, "lon": 46.68, "acc": 3.0, "t": 1763620000000 + idx, "surveyor": "م. أحمد",
                    "by": "م. أحمد", "device_id": "d1", "note": "n" + pid,
                    "cap": {"j": job["id"], "k": k, "pl": k, "ch": []}}, raw,
                   ssha=hashlib.sha256(b"s" + raw).hexdigest())
    res = arch.build_job_archive(jstore, job["id"], ledger=store.ledger)
    check(len(res["photos"]) == 3, f"P7 archive has 3 photos ({len(res['photos'])})")
    drive = os.path.join(work, "GoogleDrive", "My Drive", "Biruni")
    pids = [p["id"] for p in res["photos"]]

    def gd(pid):
        return (store.ledger.get(pid) or {}).get("places", {}).get("gdrive")

    # ---- 5. formats ------------------------------------------------------------------------------------------------
    rc = ad.archive_job_to_drive(jstore, job["id"], res, target=os.path.join(work, "GD2"), fmt="folder",
                                 level="compact", ledger=store.ledger)
    check(rc["ok"] and not any(gd(p) for p in pids),
          "a recompressed archive is never counted as a copy in the ledger (only 'original' is)")
    r = ad.archive_job_to_drive(jstore, job["id"], res, target=drive, fmt="folder", level="original", ledger=store.ledger)
    root = r["target_path"]
    check(r["ok"] and r["failed"] == 0 and os.path.isdir(root), f"folder format written: {r['written']} files")
    check(all(gd(p) for p in pids), "ledger place 'gdrive' marked for every photo after verification")
    check(ad.job_archived_to_drive(store.folder, job["id"]), "job marked archived to Drive")
    a_photo = res["photos"][0]
    with open(os.path.join(root, *a_photo["archive_rel_path"].split("/")), "rb") as f1, open(a_photo["archive_path"], "rb") as f2:
        check(f1.read() == f2.read(), "folder copy is byte-identical at 'original'")
    # idempotent: second run writes nothing
    r2 = ad.archive_job_to_drive(jstore, job["id"], res, target=drive, fmt="folder", level="original", ledger=store.ledger)
    check(r2["ok"] and r2["written"] == 0 and r2["reused"] == r["written"] + r["reused"], "folder rebuild is idempotent")
    # P7 rebuild then Drive again: still no rewrite of photos
    res_b = arch.build_job_archive(jstore, job["id"], ledger=store.ledger)
    r3 = ad.archive_job_to_drive(jstore, job["id"], res_b, target=drive, fmt="folder", level="original", ledger=store.ledger)
    check(r3["ok"] and r3["written"] <= 2, f"after a P7 rebuild only the xlsx/pdf may be rewritten (written={r3['written']})")
    check(not [n for _d, _s, fl in os.walk(drive) for n in fl if n.endswith(ad.TMP_SUFFIX)], "no temp files left behind")

    rz = ad.archive_job_to_drive(jstore, job["id"], res_b, target=drive, fmt="zip", level="original", ledger=store.ledger)
    import zipfile
    with zipfile.ZipFile(rz["target_path"]) as zf:
        check(rz["ok"] and zf.testzip() is None and len(zf.namelist()) >= 4, f"ZIP written: {len(zf.namelist())} entries")
    rz2 = ad.archive_job_to_drive(jstore, job["id"], res_b, target=drive, fmt="zip", level="original", ledger=store.ledger)
    check(rz2["ok"] and rz2["written"] == 0, "ZIP rebuild is idempotent (nothing rewritten)")

    pwd, _c = ad.ensure_password(cf)
    ra = ad.archive_job_to_drive(jstore, job["id"], res_b, target=drive, fmt="aes", level="compact",
                                 ledger=store.ledger, password=pwd)
    back = ad.read_aes_zip(ra["target_path"], pwd)
    check(ra["ok"] and ra["target_path"].endswith("BLD-2026-777.encrypted.zip"), "encrypted ZIP uses the job code name")
    check(all("/" not in n and "مبنى" not in n for n in back), f"encrypted entries are flat, job-code names only: {sorted(back)}")
    photos_back = [n for n in back if n.lower().endswith(".jpg")]
    check(len(photos_back) == 3 and all(exif_of(back[n])[0] == "BiruniCam" for n in photos_back),
          "EXIF survives inside the encrypted ZIP at 'compact'")
    ra2 = ad.archive_job_to_drive(jstore, job["id"], res_b, target=drive, fmt="aes", level="compact",
                                  ledger=store.ledger, password=pwd)
    check(ra2["ok"] and ra2["written"] == 0, "encrypted ZIP rebuild is idempotent")
    for lvl in ("original", "high", "compact"):
        rr = ad.archive_job_to_drive(jstore, job["id"], res_b, target=os.path.join(work, "lv_" + lvl), fmt="folder",
                                     level=lvl, ledger=store.ledger)
        print(f"MEASURE level={lvl}: {rr['bytes_src'] / 1e6:.2f} MB -> {rr['bytes_out'] / 1e6:.2f} MB "
              f"({100 - 100 * rr['bytes_out'] / rr['bytes_src']:.0f}% saved)", flush=True)

    # ---- 6. a truncated write is never marked ----------------------------------------------------------------------
    store2 = fs.PhotoStore(os.path.join(work, "photos2"))
    j2 = store2.jobs.create(ct.building_template(), data={"job_no": "BLD-FAIL", "client": "c"}, title="فشل",
                            loc=[46.6, 24.6], to="x", job_id="JP8FAIL")
    raw = exif_jpeg(1200, 900, 9)
    store2.save({"id": "pf-fail-1", "lat": 24.6, "lon": 46.6, "acc": 3.0, "t": 1763620000000, "surveyor": "x", "by": "x",
                 "device_id": "d", "note": "", "cap": {"j": j2["id"], "k": "*", "pl": "*", "ch": []}}, raw,
                ssha=hashlib.sha256(raw).hexdigest())
    res2 = arch.build_job_archive(store2.jobs, j2["id"], ledger=store2.ledger)
    real_write = ad._write_atomic

    def truncating(src, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as f, open(dst, "wb") as g:
            g.write(f.read()[:1000])

    drive2 = os.path.join(work, "drive2")
    for fmt in ("folder", "zip", "aes"):
        ad._write_atomic = truncating
        bad = ad.archive_job_to_drive(store2.jobs, j2["id"], res2, target=drive2, fmt=fmt, level="original",
                                      ledger=store2.ledger, password=pwd)
        ad._write_atomic = real_write
        leftovers = [n for _d, _s, fl in os.walk(drive2) for n in fl if n.endswith((".jpg", ".zip", ".pdf", ".xlsx"))]
        check(not bad["ok"] and bad["failed"] > 0 and not (store2.ledger.get("pf-fail-1") or {}).get("places", {}).get("gdrive")
              and not ad.job_archived_to_drive(store2.folder, j2["id"]) and not leftovers,
              f"{fmt}: truncated write is not marked and is removed")
    good = ad.archive_job_to_drive(store2.jobs, j2["id"], res2, target=drive2, fmt="folder", level="original",
                                   ledger=store2.ledger)
    check(good["ok"] and (store2.ledger.get("pf-fail-1") or {}).get("places", {}).get("gdrive"), "a later good write is marked")

    # ---- 7. quota ---------------------------------------------------------------------------------------------------
    import collections
    DU = collections.namedtuple("usage", "total used free")
    real_du = shutil.disk_usage
    shutil.disk_usage = lambda p: DU(1000, 920, 80)
    q = ad.quota_status(drive)
    shutil.disk_usage = lambda p: DU(1000, 800, 200)
    q2 = ad.quota_status(drive)
    shutil.disk_usage = real_du
    check(q["warn"] and abs(q["free_pct"] - 8.0) < 1e-6 and "10%" in ad.quota_message(q), "8% free -> warning")
    check(not q2["warn"], "20% free -> no warning")
    check(ad.quota_status(os.path.join(drive, "does", "not", "exist"))["free"] is not None, "quota works for a new folder")

    # ---- 8. hook + config + dock widget -----------------------------------------------------------------------------
    check(not ad.get_config()["enabled"], "option is off by default")
    check(ad.schedule_after_archive(jstore, job["id"], res_b) is None, "hook does nothing when off")
    ad.set_config(enabled=True, target=os.path.join(work, "hook_drive"), fmt="folder", level="high")
    store3 = fs.PhotoStore(os.path.join(work, "photos3"))
    j3 = store3.jobs.create(ct.building_template(), data={"job_no": "BLD-HOOK", "client": "c"}, title="خطاف",
                            loc=[46.6, 24.6], to="x", job_id="JP8HOOK")
    raw = exif_jpeg(1600, 1200, 5)
    store3.save({"id": "ph-hook-1", "lat": 24.6, "lon": 46.6, "acc": 3.0, "t": 1763620000000, "surveyor": "x", "by": "x",
                 "device_id": "d", "note": "", "cap": {"j": j3["id"], "k": "*", "pl": "*", "ch": []}}, raw,
                ssha=hashlib.sha256(raw).hexdigest())
    store3.jobs.close_job(j3["id"], ledger=store3.ledger)
    ad.wait_all()
    check(ad.job_archived_to_drive(store3.folder, j3["id"]) and not (store3.ledger.get("ph-hook-1") or {}).get("places", {}).get("gdrive"),
          "close_job hook archived to Drive in the background (level high: archived, not counted as a copy)")
    box = ad.DriveArchiveBox(None, work)
    check(box.enable.isChecked() and box.fmt.currentData() == "folder" and box.level.currentData() == "high",
          "dock box reflects the saved options")
    ad.set_config(enabled=False)

    # old ledgers (without gdrive) still load
    from biruni.field_photos.copies import CopyLedger
    old = os.path.join(work, "oldledger")
    os.makedirs(old)
    with open(os.path.join(old, "copies.jsonl"), "w", encoding="utf-8") as f:
        f.write('{"id": "z1", "sha": "a", "ssha": "", "places": {"office": 1, "archive": 2}}\n')
    check(CopyLedger(old).count("z1") == 2, "old ledger without 'gdrive' loads")
finally:
    shutil.rmtree(work, ignore_errors=True)

print("\nFAILURES: %d" % len(failures), flush=True)
for f in failures:
    print(" -", f)
app.exitQgis()
sys.exit(1 if failures else 0)
