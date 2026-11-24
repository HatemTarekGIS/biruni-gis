r"""No-loss P9: the backup engine over a fake restic (no network, no binary), its schedule and its health rows.

Tests: schedule_runs_when_due, usb_absent_defers_not_fails, failed_check_row_red, restore_mismatch_row_red,
exit3_row_amber_lists_files, password_never_in_argv_or_log, prune_refused_without_confirmation,
scheduler_never_calls_forget, wrong_password_code12_reported, not_configured_rows, dialog_not_modal,
real_restic_roundtrip (skipped unless the pinned restic is installed).

Run:  engine\bin\python-qgis.bat tests\test_backup_restic.py
"""
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backup_helpers import Checker, Env, fake_restic, other_volume  # noqa: E402
from biruni.backup import engine, health_rows, schedule, secret  # noqa: E402
from biruni.health import Ctx  # noqa: E402

check = Checker()
other_volume(True)
DAY = 86400.0


def row(e, rid, now=None):
    ctx = Ctx(get_setting=e.settings, cert_folder=e.cert, photos_folder=e.photos, now=now or __import__("time").time())
    rows = {r["id"]: r for r in health_rows._check_backup(ctx)}
    return rows.get(rid), rows


# ---- schedule_runs_when_due ------------------------------------------------------------------------------------------------
cfg = {"enabled": True, "repo": "R", "backup-hours": 24, "check-days": 7, "restore-days": 30, "vault-days": 7}
now = 1_000_000_000.0
check(schedule.due({}, cfg, now) == ["backup", "vault"], "nothing has run: back up first (check and restore need a backup)")
st = {"last_backup": {"at": now - 3600}, "last_check": {"at": now - DAY}, "last_restore": {"at": now - DAY},
      "last_vault": {"at": now - DAY}}
check(schedule.due(st, cfg, now) == [], "everything recent: nothing is due")
st["last_backup"]["at"] = now - 25 * 3600
check(schedule.due(st, cfg, now) == ["backup"], "a backup 25 h old is due")
st.update(last_check={"at": now - 8 * DAY}, last_restore={"at": now - 31 * DAY})
check(schedule.due(st, cfg, now) == ["backup", "check", "restore"], "check after 7 days, restore test after 30")
check(schedule.due(st, dict(cfg, enabled=False), now) == [] and schedule.due(st, cfg, now, present=False) == [],
      "off, or the disk is away: nothing is due")

e = Env()
e.add_photos(2)
ran = []


def sync_runner(work, done):
    try:
        res, err = work(), None
    except Exception as x:  # noqa: BLE001
        res, err = None, x
    done(res, err)


sch = schedule.Scheduler(lambda: e.engine(), e.get, e.cert, runner=sync_runner, on_result=lambda j, r, x: ran.append((j, x)))
first = sch.tick()
check(first == "backup" and ran and ran[0] == ("backup", None), f"the scheduler runs the due backup ({ran})")
check(sch.tick() in ("check", "restore", "vault"), "the next tick runs the next due job, one at a time")
check(os.path.isfile(os.path.join(e.cert, "backup", "state.json")), "state.json is written")
shutil.rmtree(e.work, ignore_errors=True)

# ---- usb_absent_defers_not_fails --------------------------------------------------------------------------------------------
e = Env(extra={"biruni/backup/repo": ""})
e.settings["biruni/backup/repo"] = os.path.join(e.work, "no-such-disk", "deeper", "repo")
e.add_photos(1)
res = e.engine().run_backup()
st = engine.load_state(e.cert)
check(res["status"] == "deferred" and "last_error_code" not in st and "last_backup" not in st and st.get("last_deferred"),
      f"an absent disk defers the run, it is not a failure ({res})")
r, _ = row(e, "backup_last")
check(r["level"] == "warn", "… and the health row stays amber (no backup yet), not red")
sch = schedule.Scheduler(lambda: e.engine(), e.get, e.cert, runner=sync_runner)
check(sch.tick() is None and not os.path.isdir(e.repo), "the scheduler starts nothing while the disk is away")
shutil.rmtree(e.work, ignore_errors=True)

# ---- failed_check_row_red -----------------------------------------------------------------------------------------------------
e = Env()
e.add_photos(2)
eng = e.engine()
eng.run_backup()
check(row(e, "backup_check")[0]["level"] == "na", "no check yet, backup is new: not rated")
open(os.path.join(e.repo, "FAIL_CHECK"), "w").close()
res = eng.run_check()
check(res["status"] == "failed", "a failing restic check is reported as failed")
check(row(e, "backup_check")[0]["level"] == "bad", "the failed check makes the row red")
os.remove(os.path.join(e.repo, "FAIL_CHECK"))
check(eng.run_check()["status"] == "ok" and row(e, "backup_check")[0]["level"] == "ok", "a good check clears it")
check(row(e, "backup_check", now=__import__("time").time() + 12 * DAY)[0]["level"] == "warn"
      and row(e, "backup_check", now=__import__("time").time() + 22 * DAY)[0]["level"] == "bad",
      "a check 10 days old is amber, 21 days red")
check(eng.run_check()["subset"].endswith("/13"), "the check reads one of 13 parts of the data")

# ---- restore_mismatch_row_red ---------------------------------------------------------------------------------------------------
items = e.add_photos(2, start=20)
eng.run_backup()
res = eng.run_restore_test()
check(res["status"] == "ok" and res["checked"] >= 2 and not os.listdir(os.path.join(e.cert, "backup", "restore-tmp")),
      f"a restore test passes and removes its temporary folder ({res})")
check(row(e, "backup_restore")[0]["level"] == "ok", "the restore row is green")
for _i, p, _s in items:      # the repository's copy rots
    with open(e.snap_file(p), "ab") as f:
        f.write(b"rot")
check(e.snap_file(items[0][1]) != "", "(the stored copy was found)")
# the restore test picks a random folder; both folders are damaged or hold marked photos: damage them all
for iid, p, _s in [(a, b, c) for a, b, c, _d in eng.photo_items()]:
    sf = e.snap_file(p)
    if sf:
        with open(sf, "ab") as f:
            f.write(b"rot")
res = eng.run_restore_test()
check(res["status"] == "failed" and res["mismatches"], f"a damaged stored copy is a mismatch ({res.get('mismatches')})")
check(row(e, "backup_restore")[0]["level"] == "bad", "any mismatch makes the restore row red")
shutil.rmtree(e.work, ignore_errors=True)

# ---- exit3_row_amber_lists_files ------------------------------------------------------------------------------------------------
e = Env()
e.add_photos(1)
open(os.path.join(e.photos, "held-open-by-another-program.locked"), "w").write("x")
res = e.engine().run_backup()
r, _ = row(e, "backup_last")
check(res["status"] == "partial" and res["code"] == 3 and r["level"] == "warn"
      and "held-open-by-another-program.locked" in r["text"], f"exit 3 is amber and names the unreadable file ({r['text']})")
check(row(e, "backup_last", now=__import__("time").time() + 3 * DAY)[0]["level"] == "warn"
      and row(e, "backup_last", now=__import__("time").time() + 8 * DAY)[0]["level"] == "bad", "a backup 2 days old stays amber, 7 days is red")

# ---- password_never_in_argv_or_log ------------------------------------------------------------------------------------------------
pw = secret.load_password(e.cert)
blob = e.calls() + "".join(open(os.path.join(e.cert, "backup", n), encoding="utf-8").read() for n in ("state.json", "log.jsonl"))
check(len(pw) >= 20 and pw not in blob, "the password is in no argument, state or log line")
check(pw not in open(secret.path(e.cert), encoding="ascii").read(), "and the password file is not plain text (DPAPI)")
shutil.rmtree(e.work, ignore_errors=True)

# ---- prune_refused_without_confirmation / scheduler_never_calls_forget ------------------------------------------------------------
e = Env()
e.add_photos(2)
eng = e.engine()
eng.run_backup()
for bad in ("", "acme office", "Acme", "yes", None):
    try:
        eng.admin_prune({"keep-daily": 7}, bad)
        check(False, f"prune refused for {bad!r}")
    except PermissionError:
        check("forget" not in e.calls(), f"prune refused for {bad!r}; restic was not asked to forget")
sch = schedule.Scheduler(lambda: e.engine(), e.get, e.cert, runner=sync_runner)
for _ in range(6):
    sch.tick()
    sch.tried.clear()
check(set(sch.calls) >= {"check", "restore", "vault"} and "forget" not in e.calls() and "prune" not in e.calls(),
      f"the scheduler ran {sorted(set(sch.calls))} and never forgot or pruned")
check(eng.admin_prune({"keep-daily": 7}, "Acme Office") is True and "forget --prune" in e.calls(),
      "with the organisation's name typed in full the administrator can prune")
shutil.rmtree(e.work, ignore_errors=True)

# ---- wrong_password_code12_reported -----------------------------------------------------------------------------------------------
e = Env()
e.add_photos(1)
eng = e.engine()
eng.run_backup()
check(row(e, "backup_password")[0]["level"] == "ok", "the password row is green while it opens the repository")
secret.install_password(e.cert, "another-password-entirely-1234")     # e.g. a PC restored from the wrong key backup
res = eng.run_backup()
st = engine.load_state(e.cert)
check(res["status"] == "error" and res["code"] == 12 and st.get("password_bad") and st.get("last_error_code") == 12,
      f"exit code 12 is reported as a wrong password ({res})")
check(row(e, "backup_password")[0]["level"] == "bad" and row(e, "backup_last")[0]["level"] == "bad",
      "the password row and the backup row are red")
check(pw not in open(os.path.join(e.cert, "backup", "log.jsonl"), encoding="utf-8").read(), "the log holds no password")
shutil.rmtree(e.work, ignore_errors=True)

# ---- not configured: amber with a second copy folder, red without ------------------------------------------------------------------
e = Env(enabled=False, extra={"biruni/backup/repo": ""})
check(row(e, "backup_last")[0]["level"] == "bad", "no backup and no second copy folder: red")
e.settings["biruni/field-photos/mirror-folder"] = os.path.join(e.work, "mirror")
check(row(e, "backup_last")[0]["level"] == "warn", "no backup but a second copy folder is set: amber")
e.settings.update({"biruni/backup/enabled": True, "biruni/backup/repo": e.repo})
os.makedirs(os.path.dirname(e.repo), exist_ok=True)
other_volume(False)
check(row(e, "backup_target")[0]["level"] == "bad", "a backup disk on the photos' volume is red")
other_volume(True)
shutil.rmtree(e.work, ignore_errors=True)

# ---- the window is not modal ---------------------------------------------------------------------------------------------------------
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
from biruni.backup.dialog import BackupDialog  # noqa: E402

dlg = BackupDialog(None)
check(not dlg.isModal() and set(dlg.btns) == {"now", "verify", "test", "restore", "prune"}, "the backup window is not modal and has its five buttons")
dlg.show()
app.processEvents()
dlg.close()

# ---- real_restic_roundtrip -----------------------------------------------------------------------------------------------------------------
from biruni.backup import tools  # noqa: E402

try:
    exe = tools.find("restic")
except tools.ToolError as x:
    exe = None
    print("SKIP real_restic_roundtrip:", x)
if exe:
    from biruni.backup.restic import Restic  # noqa: E402

    e = Env()
    e.add_photos(2)
    r = Restic(exe, e.repo, "test-password-123")
    r.init()
    res = r.backup([e.photos], "biruni")
    check(res["code"] == 0 and len(r.snapshots()) == 1 and r.check("1/2")[0], "real restic: init, backup, snapshots, check")
    shutil.rmtree(e.work, ignore_errors=True)
else:
    print("SKIP real_restic_roundtrip: the pinned restic is not installed here")
check.done()
