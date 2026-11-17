r"""Field photos through the user's own Google Drive: the intake (field_photos/drive.py, DriveIntake).

Checked: the office's folder is found under a synced root (directly, by name, one level down); a good file becomes a
photo with route "drive" and a ("photo", record) event; each file is read once (and again when it changes, so a file
still syncing is taken in when it is whole); a renamed file, a file from a phone that never scanned this office's code,
or a sealed id that does not match its header is refused; the cert decides the surveyor, never the sealed data; a
revoked surveyor key is refused and logged, and retry_refused takes it in after the key is issued again; what was seen
survives a restart; the background thread takes a file in by itself.

Run:  engine\bin\python-qgis.bat tests\test_field_photos_drive_intake.py
"""
import json
import os
import queue
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from biruni.vendor import ensure_vendor  # noqa: E402

ensure_vendor()
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from biruni.field_photos import bridge, drive  # noqa: E402
from biruni.field_photos.handover import HandoverKey, seal  # noqa: E402
from biruni.field_photos.server import PhotoStore  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


work = tempfile.mkdtemp()
pc = HandoverKey(os.path.join(work, "pc"))
k = bridge.new_key()
store = PhotoStore(os.path.join(work, "photos"))
root = os.path.join(work, "My Drive")
office = os.path.join(root, drive.folder_name(k))
os.makedirs(office)


def phone_key():
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, drive._b64u(raw)


PHONE = phone_key()


def photo():
    return b"\xff\xd8\xff\xe0" + os.urandom(2000)


def put(pid, n, sk="", dev="DEV1", meta_over=None, key_k=k, folder=None, name=None):
    meta = {"id": pid, "t": 1763370000000, "lat": 24.7, "lon": 46.7, "surveyor": "Phone says Zed"}
    meta.update(meta_over or {})
    key, pub = PHONE
    body = seal(pc.public, meta, photo())
    h = {"room": drive.room_tag(key_k), "dev": dev, "kid": drive.key_id(pc.public), "id": pid, "n": n,
         "t": meta["t"], "pub": pub, "cert": drive.cert(drive.auth_key(key_k, sk), pub, dev)}
    blob, fname = drive.pack(h, body, key)
    d = os.path.join(folder or office, "p-" + dev)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name or fname)
    with open(path, "wb") as f:
        f.write(blob)
    return path


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ---- finding the office's folder -----------------------------------------------------------------------------------
nested = os.path.join(work, "G", "Shared", drive.folder_name(k))
os.makedirs(nested)
OFFICE = os.path.normcase(os.path.abspath(office))
check(drive.office_folders([root, office, os.path.join(work, "missing")], k) == [OFFICE],
      "the folder is found under a root and when chosen itself, once")
check(drive.office_folders([work], k) == [OFFICE], "one level down is searched, deeper is not")
check(drive.office_folders([os.path.join(work, "G", "Shared")], k) == [os.path.normcase(os.path.abspath(nested))],
      "one level down (a folder kept inside another) is found")
check(drive.office_folders([root], bridge.new_key()) == [], "another office's key finds nothing here")
shutil.rmtree(os.path.join(work, "G"))

# ---- a good file ---------------------------------------------------------------------------------------------------
ev = queue.Queue()
intake = drive.DriveIntake([root], store, pc, k, ev)
p1 = put("photo-0001", 1)
r = intake.scan()
evs = drain(ev)
photos = [v for kind, v in evs if kind == "photo"]
check(r["new"] == 1 and not r["refused"] and len(photos) == 1, f"a good file becomes a photo ({r})")
rec = store.records.get("photo-0001") or {}
check(rec.get("route") == "drive" and os.path.isfile(rec.get("photo") or ""), "saved on disk with route drive")
check(any(kind == "drive" and v.get("new") == 1 for kind, v in evs), "a ('drive', summary) event for the dock")

r = intake.scan()
check(r["seen"] == 1 and r["new"] == 0 and not r["refused"], "the same file is not read again")

# ---- a file still syncing, then whole ------------------------------------------------------------------------------
p2 = put("photo-0002", 2)
with open(p2, "rb") as f:
    whole = f.read()
with open(p2, "wb") as f:
    f.write(whole[:len(whole) // 2])
r = intake.scan()
check(r["new"] == 0 and len(r["refused"]) == 1 and "photo-0002" not in store.records, "half a file is refused for now")
with open(p2, "wb") as f:
    f.write(whole)
os.utime(p2, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
r = intake.scan()
check(r["new"] == 1 and "photo-0002" in store.records, "… and taken in once it is whole")

# ---- refused files -------------------------------------------------------------------------------------------------
put("photo-0003", 3, name="000003-000000000000.bfp")
put("photo-0004", 4, dev="DEV9", folder=office)
os.rename(os.path.join(office, "p-DEV9"), os.path.join(office, "p-DEV8"))   # moved into another phone's folder
put("photo-0005", 5, key_k=bridge.new_key())                                       # certified by another office's code
put("photo-0006", 6, sk="made-up")
put("photo-0007", 7, meta_over={"id": "photo-000other"})                                 # the sealed photo is another one
r = intake.scan()
why = dict(r["refused"])
check(r["new"] == 0 and len(why) == 5 and not any(p in store.records for p in ("photo-0003", "photo-0004", "photo-0005", "photo-0006",
                                                                              "photo-0007", "photo-000other")),
      f"renamed, moved, another office, made-up key, mismatched id: all refused ({sorted(why.values())})")
check(intake.scan()["refused"] == [], "a refused file is not read again every pass")

# ---- the cert decides the surveyor ---------------------------------------------------------------------------------
ali = store.issue_surveyor_key("Ali")
put("photo-0008", 8, sk=ali["secret"], dev="DEV2", meta_over={"sk": "", "surveyor": "Someone else"})
r = intake.scan()
rec = store.records.get("photo-0008") or {}
check(r["new"] == 1 and rec.get("surveyor") == "Ali" and rec.get("device_id") == ali["id"],
      "a surveyor's own code: named from the PC's record, whatever the sealed data says")
put("photo-0009", 9, meta_over={"sk": ali["secret"]})   # the shared code, claiming Ali's key inside the sealed data
intake.scan()
check((store.records.get("photo-0009") or {}).get("surveyor") != "Ali", "a key inside the sealed data is not believed")

# ---- a revoked key, then let back in -------------------------------------------------------------------------------
store.revoke_surveyor_key(ali["id"], reason="lost phone")
put("photo-00010", 10, sk=ali["secret"], dev="DEV2")
r = intake.scan()
evs = drain(ev)
check(r["new"] == 0 and any(kind == "rejected" for kind, _ in evs) and "photo-00010" not in store.records,
      "a revoked surveyor's photo is refused and reported")
with open(os.path.join(store.folder, "rejected.jsonl"), encoding="utf-8") as f:
    check(any(json.loads(x).get("id") == "photo-00010" for x in f), "… and written to rejected.jsonl")

# ---- a blocked phone, then unblocked: retry_refused reads its files again ------------------------------------------
store.block_device("phone-xyz1", reason="test")
put("photo-00012", 12, dev="DEV3", meta_over={"device_id": "phone-xyz1"})
r = intake.scan()
check(r["new"] == 0 and "photo-00012" not in store.records, "a blocked phone's photo is refused")
store.unblock_device("phone-xyz1")
check(intake.scan()["new"] == 0, "… and not read again by itself")
intake.retry_refused()
r = intake.scan()
check("photo-00012" in store.records and "photo-00010" not in store.records,
      "retry_refused takes it in once unblocked (the revoked key stays refused)")
check(r["new"] == 1 and "photo-0001" in store.records, "… and photos already in are not read again")

# ---- a restart remembers -------------------------------------------------------------------------------------------
again = drive.DriveIntake([root], store, pc, k, queue.Queue())
r = again.scan()
check(r["new"] == 0 and r["refused"] == [] and r["seen"] >= 10, "after a restart nothing is read again")
other = drive.DriveIntake([root], store, pc, bridge.new_key(), queue.Queue())
check(other.seen == {}, "a different office key does not reuse what was seen")

# ---- the background thread -----------------------------------------------------------------------------------------
bg_ev = queue.Queue()
bg = drive.DriveIntake([root], store, pc, k, bg_ev, interval=0.2)
bg.start()
put("photo-00011", 11)
deadline = time.time() + 10
while time.time() < deadline and "photo-00011" not in store.records:
    time.sleep(0.1)
bg.stop()
check("photo-00011" in store.records and bg.thread is None, "the thread takes a new file in by itself and stops")

shutil.rmtree(work, ignore_errors=True)
print(f"\n{'FAILED ' + str(len(failures)) if failures else 'ALL PASSED'}")
sys.exit(1 if failures else 0)
