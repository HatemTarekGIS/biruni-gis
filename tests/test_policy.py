r"""No-loss P5: signed tracking policy (field_photos/policy.py).

Tests:
- sign and verify
- rollback, wrong signer and another room are refused (plus no-pin refused: no TOFU)
- canonical_contains_no_secret
- seq_survives_reinstall (next_seq floor and key backup v2 merge)
- effective precedence (`per[tag] ?? jobs[job] ?? mode`), RANK, and hours with `tz`

Run:  engine\bin\python-qgis.bat tests\test_policy.py
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from biruni.field_photos import bridge, handover, policy, recipients  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def refused(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except policy.PolicyError as e:
        return str(e)
    return ""


class FakeSettings:
    def __init__(self):
        self.d = {}

    def value(self, k, default=None):
        return self.d.get(k, default)

    def setValue(self, k, v):
        self.d[k] = v


work = tempfile.mkdtemp(prefix="biruni_p5_policy_")
try:
    folder = os.path.join(work, "cert")
    hk = HandoverKey(folder)
    key = bridge.new_key()
    sk, pin = bridge.identity(key)
    room = key[:12]

    # Before the office saves a policy, signed() returns None
    check(policy.signed(folder, key) is None, "no policy before the office saves one (existing rooms keep toggle)")

    # Save a policy with per-surveyor and per-job overrides
    skeys = {
        "qr_mode": "per_surveyor",
        "surveyors": {
            "srv_1": {"id": "srv_1", "name": "Hatem", "secret": "TopSecretSurveyorKey_1", "revoked": False},
            "srv_2": {"id": "srv_2", "name": "Revoked", "secret": "RevokedSecretKey_2", "revoked": True},
        },
    }
    doc1 = policy.set_policy(
        folder,
        mode="15",
        hours=[[0, 480, 1020], [1, 480, 1020]],
        tz=180,
        per_ids={"srv_1": "1", "srv_2": "live"},
        jobs={"job_pole_1": "5"},
    )
    check(doc1["seq"] >= int(time.time() // 60), f"seq uses next_seq floor ({doc1['seq']})")

    pl = policy.signed(folder, key, surveyor_keys=skeys)
    check(isinstance(pl, dict) and all(k in pl for k in ("l", "ip", "is")), "signed() returns envelope {l, ip, is}")

    # 1. sign and verify
    lst = policy.verify(pl, room, pin=pin)
    tag1 = policy.sk_tag("TopSecretSurveyorKey_1")
    check(lst["v"] == 1 and lst["seq"] == doc1["seq"] and lst["room"] == room and lst["mode"] == "15"
          and lst["per"] == {tag1: "1"} and lst["jobs"] == {"job_pole_1": "5"},
          f"sign and verify ({lst})")

    # 2. canonical_contains_no_secret
    check("TopSecretSurveyorKey_1" not in pl["l"]
          and "RevokedSecretKey_2" not in pl["l"]
          and "srv_1" not in pl["l"]
          and tag1 in pl["l"],
          "canonical_contains_no_secret (only sha256 tag is published; revoked surveyor omitted)")

    # 3. rollback, wrong signer, no pin (no TOFU), another room, and tampered payload are refused
    check(refused(policy.verify, pl, room, pin=pin, last_seq=doc1["seq"] + 1) == "rollback",
          "rollback (older seq) is refused")
    check(refused(policy.verify, pl, room, pin=None) == "no pin"
          and refused(policy.verify, pl, room, pin="") == "no pin",
          "no TOFU: missing pin refuses policy")
    other_sk, other_pin = bridge.identity(bridge.new_key())
    forged_ip, forged_sig = recipients.sign_with(other_sk, pl["l"], prefix=policy.SIGN_PREFIX)
    check(refused(policy.verify, {"l": pl["l"], "ip": forged_ip, "is": forged_sig}, room, pin=pin) == "wrong signer",
          "wrong signer is refused")
    # Signed with BRRL1 prefix instead of BRPL1 is refused
    wrong_prefix_ip, wrong_prefix_sig = recipients.sign_with(sk, pl["l"], prefix="BRRL1|")
    check(refused(policy.verify, {"l": pl["l"], "ip": wrong_prefix_ip, "is": wrong_prefix_sig}, room, pin=pin) == "bad signature",
          "signature with wrong domain prefix is refused")
    check(refused(policy.verify, pl, "otherroom123", pin=pin) == "another room",
          "another room is refused")
    tampered = dict(pl, l=pl["l"].replace('"mode":"15"', '"mode":"live"'))
    check(refused(policy.verify, tampered, room, pin=pin) == "bad signature",
          "tampered policy JSON is refused")

    # Bounds checks on verify (>21 hours, >200 per, >8000 bytes)
    too_many_hours = dict(lst, hours=[[i % 7, 60, 120] for i in range(22)])
    l_tmh = policy.canonical(too_many_hours)
    ip_h, sig_h = recipients.sign_with(sk, l_tmh, prefix=policy.SIGN_PREFIX)
    check(refused(policy.verify, {"l": l_tmh, "ip": ip_h, "is": sig_h}, room, pin=pin) == "invalid hours",
          "more than 21 hour rows refused")

    # 4. seq_survives_reinstall (floor + key backup v2 restore)
    old_doc = dict(policy.load(folder), seq=10)
    policy.save(folder, old_doc)
    doc2 = policy.set_policy(folder, mode="5", hours=[], tz=180)
    check(doc2["seq"] >= doc1["seq"] and doc2["seq"] >= int(time.time() // 60) and doc2["seq"] > 10,
          f"seq_survives_reinstall: next_seq jumps above minutes-since-1970 ({doc2['seq']} >= {doc1['seq']})")

    st = FakeSettings()
    bkey = bridge.load_key(st)
    bak_path = os.path.join(work, "office.biruni-key")
    PW = "Field-Office 2026 keys!"
    handover.backup_v2(folder, bak_path, PW, settings=st)

    # New PC with older seq: restoring v2 backup brings the policy back with a higher seq
    new_pc_dir = os.path.join(work, "new_pc")
    new_st = FakeSettings()
    handover.restore(new_pc_dir, bak_path, PW, settings=new_st)
    restored_pl = policy.load(new_pc_dir)
    check(restored_pl["mode"] == "5" and restored_pl["seq"] > doc2["seq"],
          f"key backup v2 restores policy and advances seq ({restored_pl['seq']} > {doc2['seq']})")

    # If the target PC already has a newer policy seq, higher seq wins on restore
    newer_doc = policy.set_policy(new_pc_dir, mode="photo", hours=[[2, 540, 960]], tz=120)
    handover.restore(new_pc_dir, bak_path, PW, settings=new_st)
    after_old_restore = policy.load(new_pc_dir)
    check(after_old_restore["mode"] == "photo" and after_old_restore["seq"] > newer_doc["seq"],
          f"restoring an older backup keeps the newer policy (higher seq wins: {after_old_restore})")

    # 5. effective precedence (per[tag] ?? jobs[job] ?? mode), RANK, and hours with tz
    check(policy.RANK["off"] < policy.RANK["photo"] < policy.RANK["15"] < policy.RANK["5"] < policy.RANK["1"] < policy.RANK["live"],
          "RANK ordering is off < photo < 15 < 5 < 1 < live")

    # Sunday 2026-11-22 07:30 UTC -> in UTC+3 (tz=180) it is Sunday 10:30 (630 min), inside [0, 480, 1020]
    sun_0730_utc = int(dt.datetime(2026, 11, 22, 7, 30, tzinfo=dt.timezone.utc).timestamp() * 1000)
    # Sunday 2026-11-22 15:30 UTC -> in UTC+3 (tz=180) it is Sunday 18:30 (1110 min), outside [0, 480, 1020]
    sun_1530_utc = int(dt.datetime(2026, 11, 22, 15, 30, tzinfo=dt.timezone.utc).timestamp() * 1000)
    # Friday 2026-11-27 07:30 UTC -> day=5, not in hours [[0, 480, 1020], [1, 480, 1020]]
    fri_0730_utc = int(dt.datetime(2026, 11, 27, 7, 30, tzinfo=dt.timezone.utc).timestamp() * 1000)

    sample = {
        "v": 1,
        "seq": 100,
        "room": room,
        "mode": "15",
        "hours": [[0, 480, 1020], [1, 480, 1020]],
        "tz": 180,
        "per": {tag1: "1"},
        "jobs": {"job_pole_1": "5"},
    }
    check(policy.effective(sample, tag="", job="", now_ms=sun_0730_utc) == "15",
          "effective: default mode inside work hours")
    check(policy.effective(sample, tag="", job="job_pole_1", now_ms=sun_0730_utc) == "5",
          "effective: job override beats default mode")
    check(policy.effective(sample, tag=tag1, job="job_pole_1", now_ms=sun_0730_utc) == "1",
          "effective: per-surveyor tag override beats job and default mode")
    check(policy.effective(sample, tag=tag1, job="job_pole_1", now_ms=sun_1530_utc) == "off",
          "effective: falls to 'off' outside work hours (computed with tz)")
    check(policy.effective(sample, tag=tag1, job="job_pole_1", now_ms=fri_0730_utc) == "off",
          "effective: falls to 'off' on non-work day")
    always_sample = dict(sample, hours=[])
    check(policy.effective(always_sample, tag="", job="", now_ms=fri_0730_utc) == "15",
          "effective: empty hours means always")

finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
