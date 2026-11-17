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

shutil.rmtree(work, ignore_errors=True)
print(f"\n{'FAILED ' + str(len(failures)) if failures else 'ALL PASSED'}")
sys.exit(1 if failures else 0)
