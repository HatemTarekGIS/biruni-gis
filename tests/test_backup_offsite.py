r"""No-loss P9 stage 9d: rclone off-site copy of the restic repository (no network, no real binary).

Tests:
- copy_immutable_never_sync;
- config_pass_in_env_only;
- failure_row_red;
- key_backup_carries_rclone_pw.

Run:  engine\bin\python-qgis.bat tests\test_backup_offsite.py
"""
import glob
import json
import os
import shutil
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backup_helpers import PY_ENV, Checker, Env, other_volume  # noqa: E402
from biruni.backup import engine, health_rows, offsite, schedule  # noqa: E402
from biruni.field_photos import bridge, handover  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402
from biruni.field_photos.tunnel import unprotect  # noqa: E402
from biruni.health import Ctx  # noqa: E402

check = Checker()
other_volume(True)
DAY = 86400.0

FAKE_RCLONE = """import json, os, sys
args = sys.argv[1:]
cfg = args[args.index("--config") + 1] if "--config" in args else ""
log_dir = os.path.dirname(cfg) if cfg else "."
os.makedirs(log_dir, exist_ok=True)
pw = os.environ.get("RCLONE_CONFIG_PASS", "")
rec = {"argv": args, "has_env_pw": bool(pw), "pw_len": len(pw), "leaked_env": [k for k in os.environ if k.startswith("SECRET_")]}
open(os.path.join(log_dir, "rclone_calls.jsonl"), "a", encoding="utf-8").write(json.dumps(rec) + "\\n")
if os.path.exists(os.path.join(log_dir, "FAIL_RCLONE")):
    sys.stderr.write(f"Fatal: remote error (pass={pw})\\n")
    sys.exit(2)
sys.exit(0)
"""


class FakeSettings:
    def __init__(self):
        self.d = {}

    def value(self, k, default=None):
        return self.d.get(k, default)

    def setValue(self, k, v):
        self.d[k] = v


def make_fake_rclone(work, cert):
    p = os.path.join(work, "fake_rclone.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write(FAKE_RCLONE)
    return offsite.Rclone([sys.executable, p], cert_folder=cert, env=PY_ENV)


# ---- copy_immutable_never_sync ---------------------------------------------------------------------------------------------
e = Env(extra={"biruni/backup/offsite-remote": "office-cloud:biruni-backup"})
e.add_photos(2)
eng = e.engine(rclone_factory=lambda cert: make_fake_rclone(e.work, cert))
eng.run_backup()
res = eng.run_offsite()
calls_path = os.path.join(e.cert, "backup", "rclone_calls.jsonl")
calls = [json.loads(line) for line in open(calls_path, encoding="utf-8")]
argv0 = calls[-1]["argv"]
check(res["status"] == "ok" and "copy" in argv0 and "--immutable" in argv0
      and e.repo in argv0 and "office-cloud:biruni-backup" in argv0
      and "sync" not in argv0 and "delete" not in argv0 and "purge" not in argv0,
      f"copy_immutable_never_sync: rclone runs copy --immutable <repo> <remote> ({argv0})")

rc_guard = make_fake_rclone(e.work, e.cert)
for forbidden in (["sync", e.repo, "office-cloud:x"], ["delete", "office-cloud:x"], ["purge", "office-cloud:x"]):
    try:
        rc_guard.run(forbidden)
        check(False, f"forbidden command {forbidden[0]} must be rejected")
    except ValueError:
        check(True, f"forbidden command {forbidden[0]} is rejected before spawning")

# Also verify scheduler runs offsite when configured and never calls sync
st_now = engine.load_state(e.cert)
st_now["last_offsite"]["at"] -= 25 * 3600
engine.save_state(e.cert, st_now)
due_jobs = schedule.due(engine.load_state(e.cert), engine.config(e.get), __import__("time").time())
check("offsite" in due_jobs, f"scheduler queues offsite when due ({due_jobs})")

# ---- config_pass_in_env_only -----------------------------------------------------------------------------------------------
os.environ["SECRET_SHOULD_NOT_LEAK"] = "top-secret-123"
try:
    eng.run_offsite()
finally:
    os.environ.pop("SECRET_SHOULD_NOT_LEAK", None)
rpw = offsite.load_password(e.cert)
calls = [json.loads(line) for line in open(calls_path, encoding="utf-8")]
last_call = calls[-1]
state_raw = open(os.path.join(e.cert, "backup", "state.json"), encoding="utf-8").read()
log_raw = open(os.path.join(e.cert, "backup", "log.jsonl"), encoding="utf-8").read()
argv_joined = " ".join(last_call["argv"])
check(len(rpw) >= 20 and last_call["has_env_pw"] and last_call["pw_len"] == len(rpw),
      "config_pass_in_env_only: RCLONE_CONFIG_PASS is passed in the child's environment")
check(rpw not in argv_joined and rpw not in state_raw and rpw not in log_raw,
      "RCLONE_CONFIG_PASS never appears in argv, state.json or log.jsonl")
check(not last_call["leaked_env"], "child process runs with a clean environment")
raw_dpapi = open(offsite.pw_path(e.cert), encoding="ascii").read().strip()
check(rpw not in raw_dpapi and unprotect(raw_dpapi) == rpw, "rclone_pw.dat on disk is DPAPI-protected")

# ---- failure_row_red -------------------------------------------------------------------------------------------------------
now = __import__("time").time()
ctx = Ctx(get_setting=e.settings, cert_folder=e.cert, photos_folder=e.photos, now=now)
rows = {r["id"]: r for r in health_rows._check_backup(ctx)}
check(rows.get("offsite", {}).get("level") == "ok", "offsite row is green after a successful copy")

ctx_10d = Ctx(get_setting=e.settings, cert_folder=e.cert, photos_folder=e.photos, now=now + 10 * DAY)
check({r["id"]: r for r in health_rows._check_backup(ctx_10d)}["offsite"]["level"] == "warn",
      "offsite row is amber after 7 days")
ctx_35d = Ctx(get_setting=e.settings, cert_folder=e.cert, photos_folder=e.photos, now=now + 35 * DAY)
check({r["id"]: r for r in health_rows._check_backup(ctx_35d)}["offsite"]["level"] == "bad",
      "offsite row is red after 30 days")

open(os.path.join(e.cert, "backup", "FAIL_RCLONE"), "w").close()
fail_res = eng.run_offsite()
rows_fail = {r["id"]: r for r in health_rows._check_backup(ctx)}
state_after_fail = open(os.path.join(e.cert, "backup", "state.json"), encoding="utf-8").read()
log_after_fail = open(os.path.join(e.cert, "backup", "log.jsonl"), encoding="utf-8").read()
check(fail_res["status"] == "error" and rows_fail.get("offsite", {}).get("level") == "bad",
      f"failure_row_red: a failed off-site copy makes the offsite health row red ({rows_fail.get('offsite')})")
check(rpw not in state_after_fail and rpw not in log_after_fail and rpw not in fail_res.get("error", ""),
      "even when stderr echoes the password, it is scrubbed from error, state.json and log.jsonl")

# ---- key_backup_carries_rclone_pw ------------------------------------------------------------------------------------------
STRONG = "Field-Office 2026 keys!"
st = FakeSettings()
bridge.load_key(st)
HandoverKey(e.cert)
bak = os.path.join(e.work, "office-v2.biruni-key")
handover.backup_v2(e.cert, bak, STRONG, st)
bak_raw = open(bak, encoding="utf-8").read()
check(rpw not in bak_raw, "the rclone password is sealed inside the v2 key backup, not plaintext")

new_pc = os.path.join(e.work, "new-pc")
handover.restore(new_pc, bak, STRONG, FakeSettings())
check(offsite.load_password(new_pc) == rpw,
      "key_backup_carries_rclone_pw: restoring v2 key backup on a new PC restores rclone_pw")

other_pc = os.path.join(e.work, "other-pc")
os.makedirs(other_pc, exist_ok=True)
other_pw, _ = offsite.ensure_password(other_pc)
check(other_pw != rpw, "(other PC starts with a different rclone password)")
handover.restore(other_pc, bak, STRONG, FakeSettings())
olds = glob.glob(offsite.pw_path(other_pc) + ".*.old")
check(offsite.load_password(other_pc) == rpw and len(olds) == 1
      and unprotect(open(olds[0], encoding="ascii").read().strip()) == other_pw,
      "restoring over a different rclone password keeps the old one as .old")

shutil.rmtree(e.work, ignore_errors=True)
check.done()
