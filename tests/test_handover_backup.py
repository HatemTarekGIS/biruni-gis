r"""This computer's photo key: its password-locked backup, restoring it, and a key file that cannot be read
(field_photos/handover.py), plus the intake's lock and forget_refused (field_photos/drive.py).

Run:  engine\bin\python-qgis.bat tests\test_handover_backup.py
"""
import glob
import json
import os
import queue
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from biruni.field_photos import bridge, drive, handover  # noqa: E402
from biruni.field_photos.handover import HandoverKey, seal  # noqa: E402
from biruni.field_photos.server import PhotoStore  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def refuses(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


work = tempfile.mkdtemp()
pc_dir = os.path.join(work, "pc")
pc = HandoverKey(pc_dir)
PW = "a long backup password 7"
bak = os.path.join(work, "key.biruni-key")
pub = handover.backup(pc_dir, bak, PW)
text = open(bak, encoding="utf-8").read()
check(pub == pc.public and "PRIVATE KEY" not in text and json.loads(text)["pub"] == pub,
      "the backup holds the public key in clear and the private key only sealed")
check(handover.backup_info(bak)["pub"] == pub and handover.backup_info(bak)["made"], "its key and date read without a password")

blob = seal(pc.public, {"id": "photo-0001"}, b"\xff\xd8\xff" + b"x" * 100)
new_dir = os.path.join(work, "new-pc")
check(handover.restore(new_dir, bak, PW) == pub and HandoverKey(new_dir).public == pub,
      "restored on a new computer: the same key")
check(HandoverKey(new_dir).open(blob)[0]["id"] == "photo-0001", "… and it opens a photo sealed for the old one")

# wrong password, a changed file, not a backup: refused, nothing touched
other_dir = os.path.join(work, "other")
other = HandoverKey(other_dir)
before = open(os.path.join(other_dir, handover.FILE), encoding="ascii").read()
check(refuses(handover.restore, other_dir, bak, "wrong password"), "a wrong password is refused")
d = json.loads(text)
d["pub"] = other.public
bad = os.path.join(work, "bad.biruni-key")
json.dump(d, open(bad, "w", encoding="utf-8"))
check(refuses(handover.restore, other_dir, bad, PW), "a backup whose public key was changed is refused")
open(bad, "w", encoding="utf-8").write("{}")
check(refuses(handover.restore, other_dir, bad, PW) and refuses(handover.backup_info, bad), "not a backup: refused")
check(open(os.path.join(other_dir, handover.FILE), encoding="ascii").read() == before
      and not glob.glob(os.path.join(other_dir, "*.old")), "… and the key there is untouched")

# over a different key: that key is kept beside it
check(handover.restore(other_dir, bak, PW) == pub and HandoverKey(other_dir).public == pub,
      "restoring over another key makes the backup's key the one in use")
olds = glob.glob(os.path.join(other_dir, handover.FILE + ".*.old"))
check(len(olds) == 1 and open(olds[0], encoding="ascii").read() == before, "… and the other key is kept as .old")
check(handover.restore(other_dir, bak, PW) == pub and len(glob.glob(os.path.join(other_dir, "*.old"))) == 1,
      "restoring the key already in use changes nothing")

# a key file that cannot be read is moved aside, not written over
broken_dir = os.path.join(work, "broken")
os.makedirs(broken_dir)
open(os.path.join(broken_dir, handover.FILE), "w", encoding="ascii").write("not a key")
HandoverKey(broken_dir)
aside = glob.glob(os.path.join(broken_dir, handover.FILE + ".unreadable-*"))
check(len(aside) == 1 and open(aside[0], encoding="ascii").read() == "not a key",
      "an unreadable key file is moved aside before a new key is made")

# ---- drive.suggest_roots -------------------------------------------------------------------------------------------
gd = os.path.join(work, "G")
os.makedirs(os.path.join(gd, "My Drive"))
os.makedirs(os.path.join(gd, ".shortcut-targets-by-id"))
os.makedirs(os.path.join(work, "home", "Google Drive"))
got = drive.suggest_roots(volumes=[(gd, "Google Drive"), (work, "Data")], home=os.path.join(work, "home"))
check(got == [os.path.join(gd, "My Drive"), os.path.join(work, "home", "Google Drive")],
      f"suggested: My Drive on the Google Drive volume and the home mirror ({got})")

# ---- the intake's lock and forget_refused --------------------------------------------------------------------------
k = bridge.new_key()
store = PhotoStore(os.path.join(work, "photos"))
office = os.path.join(work, "root", drive.folder_name(k), "p-DEV1")
os.makedirs(office)
for i in range(1, 41):
    open(os.path.join(office, f"{i:06d}-{i:012x}.bfp"), "wb").write(b"junk")
it = drive.DriveIntake([os.path.join(work, "root")], store, pc, k, queue.Queue())
errors = []


def poke():
    try:
        for _ in range(200):
            it.retry_refused()
    except Exception as e:  # noqa: BLE001
        errors.append(e)


t = threading.Thread(target=poke)
t.start()
for _ in range(20):
    it.scan()
t.join()
seen = json.load(open(os.path.join(store.folder, drive.SEEN_NAME), encoding="utf-8"))
check(not errors and isinstance(seen.get("files"), dict), "retry_refused from another thread during scans: no error")
r = it.scan()
check(r["folders"] == 1 and len(r["refused"]) <= 40, "the summary says how many office folders were found")
it.scan()
drive.forget_refused(store.folder)
seen = json.load(open(os.path.join(store.folder, drive.SEEN_NAME), encoding="utf-8"))
check(seen["files"] == {} and seen["room"] == drive.room_tag(k), "forget_refused clears the refused files, keeps the rest")

# ---- key backup v2 (no-loss P3): the whole office on a new PC ------------------------------------------------------
from biruni.field_photos import recipients  # noqa: E402
from biruni.field_photos.ladder import Ladder  # noqa: E402
from biruni.field_photos.tunnel import unprotect  # noqa: E402


class FakeSettings:
    """A settings store of one Windows account (QgsSettings' value / setValue)."""

    def __init__(self):
        self.d = {}

    def value(self, k, default=None):
        return self.d.get(k, default)

    def setValue(self, k, v):
        self.d[k] = v


old_st = FakeSettings()
k_fp = bridge.load_key(old_st)
k_gps = bridge.load_key(old_st, name="biruni/gps/bridge-key")
old_pin_fp = bridge.identity(k_fp, settings=old_st)[1]
old_pin_gps = bridge.identity(k_gps, settings=old_st)[1]
v2_dir = os.path.join(work, "v2-office")
v2_office = HandoverKey(v2_dir)
backup_pc = HandoverKey(os.path.join(work, "v2-backup"))
recipients.add(v2_dir, backup_pc.public, "backup", "branch", office_pub=v2_office.public)
url_before = f"#k={k_fp}&i={old_pin_fp}&pk={v2_office.public}"
bak2 = os.path.join(work, "office.biruni-key")
check(refuses(handover.backup_v2, v2_dir, bak2, "short1", old_st) and not os.path.exists(bak2),
      "v2: a weak password is refused (administrator rule) and nothing is written")
check(refuses(handover.backup_v2, v2_dir, bak2, "password1234", old_st), "v2: a common password is refused")
STRONG = "Field-Office 2026 keys!"
from biruni.field_photos.tunnel import protect as _protect  # noqa: E402
open(os.path.join(v2_dir, "drive_archive_pw.dat"), "w", encoding="ascii").write(_protect("ABCDE-FGHJK-LMNPQ"))
check(handover.backup_v2(v2_dir, bak2, STRONG, old_st) == v2_office.public, "v2 backup written")
raw2 = open(bak2, encoding="utf-8").read()
check(json.loads(raw2)["v"] == 2 and k_fp not in raw2 and "PRIVATE" not in raw2 and handover.backup_info(bak2)["v"] == 2,
      "v2 holds the bridge keys and identities only sealed")

# positions and a photo sealed before the disaster
lad_old = Ladder(k_fp)
env = lad_old.seal({"t": "lpos", "v": 1, "d": "dev_test_1234", "e": "ep1", "lo": 1, "bl": 1,
                    "b": [[1, {"id": "dev_test_1234", "lat": 30.0, "lon": 31.0, "t": 1763370000000}]],
                    "n": "nonce-v2", "ts": 1763370000000})
photo2 = seal(v2_office.public, {"id": "photo-v2"}, b"\xff\xd8" + b"y" * 50)

new_st = FakeSettings()           # a new PC: a fresh settings store and a fresh folder
new_dir = os.path.join(work, "v2-new-pc")
check(refuses(handover.restore, new_dir, bak2, "wrong password", new_st) and not new_st.d,
      "v2: a wrong password changes nothing")
check(handover.restore(new_dir, bak2, STRONG, new_st) == v2_office.public, "v2 restored on a new PC")
nk_fp = unprotect(new_st.value("biruni/field-photos/bridge-key", ""))
nk_gps = unprotect(new_st.value("biruni/gps/bridge-key", ""))
check(nk_fp == k_fp and nk_gps == k_gps, "the same bridge keys (the same QR codes)")
check(bridge.identity(nk_fp, settings=new_st)[1] == old_pin_fp and bridge.identity(nk_gps, settings=new_st)[1]
      == old_pin_gps, "the same pins (&i=): phones trust the restored PC without a new scan")
url_after = f"#k={nk_fp}&i={bridge.identity(nk_fp, settings=new_st)[1]}&pk={HandoverKey(new_dir).public}"
check(url_after == url_before, "the phone link is exactly the same")
pos = Ladder(nk_fp).open(env)
check(isinstance(pos, dict) and pos.get("b", [[0, {}]])[0][1].get("lat") == 30.0, "it opens the positions")
check(HandoverKey(new_dir).open(photo2)[0]["id"] == "photo-v2", "it opens the photos")
check(unprotect(open(os.path.join(new_dir, "drive_archive_pw.dat"), encoding="ascii").read().strip())
      == "ABCDE-FGHJK-LMNPQ" and "ABCDE" not in raw2, "the Drive archive's ZIP password comes back too (sealed in the file)")
check([e["p"] for e in recipients.load(new_dir)["r"]] == [backup_pc.public] and
      recipients.load(new_dir)["seq"] > recipients.load(v2_dir)["seq"],
      "the recipients list comes back, with a higher seq (phones never see a rollback)")
# a PC that already had a newer list keeps it (no rollback through a restore)
recipients.remove(new_dir, backup_pc.public)
recipients.add(new_dir, backup_pc.public, "backup", "again", office_pub=v2_office.public)
seq_now = recipients.load(new_dir)["seq"]
handover.restore(new_dir, bak2, STRONG, new_st)
check(recipients.load(new_dir)["seq"] > seq_now and len(recipients.load(new_dir)["r"]) == 1
      and recipients.load(new_dir)["r"][0]["n"] == "again", "restoring an older backup keeps the newer list, seq rises")
# a PC that had its own other bridge key keeps it aside
other_st = FakeSettings()
mine = bridge.load_key(other_st)
handover.restore(os.path.join(work, "v2-other"), bak2, STRONG, other_st)
check(unprotect(other_st.value("biruni/field-photos/bridge-key-before-restore", "")) == mine,
      "a PC's own different bridge key is kept aside (…-before-restore)")
# v1 files still restore, and on a v2-aware PC they touch only the handover key
v1_st = FakeSettings()
check(handover.restore(os.path.join(work, "v1-again"), bak, PW, v1_st) == pub and not v1_st.d,
      "a v1 backup still restores (the handover key only)")

# ---- P6 (D2): key_backup_carries_mb_secret ------------------------------------------------------------------------
from biruni.field_photos import mailbox  # noqa: E402

mb_old_st = FakeSettings()
_ = bridge.load_key(mb_old_st)
mb_secret_val = "M" * 43
mailbox.save_config(mb_secret_val, 14, settings=mb_old_st)
mb_dir = os.path.join(work, "v2-mb-office")
mb_hk = HandoverKey(mb_dir)
bak_mb = os.path.join(work, "office-mb.biruni-key")
check(handover.backup_v2(mb_dir, bak_mb, STRONG, mb_old_st) == mb_hk.public, "v2 backup with mb_secret written")
raw_mb = open(bak_mb, encoding="utf-8").read()
check(mb_secret_val not in raw_mb, "mb_secret is sealed inside v2 backup and never in clear")

mb_new_st = FakeSettings()
mailbox.save_config("", 30, settings=mb_new_st, on=False)
mb_new_dir = os.path.join(work, "v2-mb-restored")
check(handover.restore(mb_new_dir, bak_mb, STRONG, mb_new_st) == mb_hk.public, "v2 backup with mb_secret restored")
check(mailbox.load_secret(mb_new_st) == mb_secret_val
      and mailbox.load_days(mb_new_st) == 30
      and mb_secret_val not in str(mb_new_st.value(mailbox.SECRET_KEY, "")),
      "key_backup_carries_mb_secret: restores mb_secret DPAPI-protected while keeping stored days")

shutil.rmtree(work, ignore_errors=True)
print(f"\n{'FAILED ' + str(len(failures)) if failures else 'ALL PASSED'}")
sys.exit(1 if failures else 0)
