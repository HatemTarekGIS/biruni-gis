r"""No-loss P9 stage 9b: a backup PC takes in the office's Google Drive photos (.bfp) and positions (.bgp) by itself, with the
office pack, and never changes the Drive folder.

Tests: auto_intake_takes_bfp_and_bgp_with_pack, never_deletes_or_writes_in_drive_folder, no_pack_no_intake,
refused_file_counted_in_health, surveyors_from_the_pack (a per-surveyor code the pack carries).

Run:  engine\bin\python-qgis.bat tests\test_backup_intake.py
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backup_helpers import Checker  # noqa: E402
from biruni.vendor import ensure_vendor  # noqa: E402

ensure_vendor()
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from biruni.backup import health_rows  # noqa: E402
from biruni.field_photos import backup_intake, bridge, drive, envelope, recipients  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402
from biruni.field_photos.ladder import Ladder  # noqa: E402
from biruni.field_photos.server import PhotoStore  # noqa: E402
from biruni.field_photos.tunnel import protect  # noqa: E402
from biruni.health import Ctx  # noqa: E402

check = Checker()
work = tempfile.mkdtemp(prefix="biruni_p9_intake_")
office = HandoverKey(os.path.join(work, "office-cert"))
backup_cert = os.path.join(work, "backup-cert")
backup = HandoverKey(backup_cert)
k_fp, k_gps = bridge.new_key(), bridge.new_key()
KEYS = {"biruni/field-photos/bridge-key": k_fp, "biruni/gps/bridge-key": k_gps}
root = os.path.join(work, "My Drive")
ofolder = os.path.join(root, drive.folder_name(k_fp))
gfolder = os.path.join(root, drive.folder_name(k_gps))
os.makedirs(ofolder)
os.makedirs(gfolder)


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def put_photo(pid, n, key=k_fp, folder=ofolder, sk="", name_override=None):
    ph = ec.generate_private_key(ec.SECP256R1())
    ppub = b64u(ph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    meta = {"id": pid, "t": 1763370000000, "lat": 24.7, "lon": 46.7, "surveyor": "Phone"}
    body = envelope.seal([office.public, backup.public], meta, b"\xff\xd8\xff\xe0" + os.urandom(500))
    h = {"room": drive.room_tag(key), "dev": "DEV1", "kid": office.kid, "id": pid, "n": n, "t": meta["t"], "pub": ppub,
         "cert": drive.cert(drive.auth_key(key, sk), ppub, "DEV1")}
    blob, name = drive.pack(h, body, ph)
    d = os.path.join(folder, "p-DEV1")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name_override or name)
    open(p, "wb").write(blob)
    return p


def put_pos(n):
    env = Ladder(k_gps).seal({"t": "lpos", "v": 1, "d": "devG01", "e": "ep1", "lo": 1, "bl": 1,
                              "b": [[1, {"id": "devG01", "lat": 30.2, "lon": 31.3, "t": int(time.time() * 1000)}]],
                              "n": "nonce-%d" % n, "ts": int(time.time() * 1000)})
    d = os.path.join(gfolder, "g-devG01")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%09d-%09d-abc123.bgp" % (n, n))
    open(p, "wb").write(b"BGP1\n" + env.encode("ascii"))
    return p


def tree(d):
    return sorted((os.path.join(r, f), os.stat(os.path.join(r, f)).st_size, os.stat(os.path.join(r, f)).st_mtime_ns)
                  for r, _d, fs in os.walk(d) for f in fs) + sorted(os.path.join(r, x) for r, ds, _f in os.walk(d) for x in ds)


# the pack, as the "Backup PC" window keeps it on the backup PC
blob = recipients.office_pack(backup.public, KEYS, {"qr_mode": "shared", "surveyors": {}}, office_pub=office.public)
open(os.path.join(backup_cert, backup_intake.PACK_FILE), "w", encoding="ascii").write(protect(base64.b64encode(blob).decode("ascii")))
pack = backup_intake.load_pack(backup_cert, backup)
check(pack is not None and set(pack["keys"].values()) == {k_fp, k_gps}, "the saved office pack opens on the backup PC")

# ---- auto_intake_takes_bfp_and_bgp_with_pack ------------------------------------------------------------------------------------------
store = PhotoStore(os.path.join(work, "backup-photos"))
p1 = put_photo("bkphoto-0001", 1)
p2 = put_photo("bkphoto-0002", 2)
pos = put_pos(1)
before = tree(root)
bi = backup_intake.BackupIntake([root], store, backup, pack, state_folder=backup_cert)
res = bi.scan_once()
check(res["new"] == 2 and "bkphoto-0001" in store.records and "bkphoto-0002" in store.records, f"the .bfp photos are saved on the backup PC ({res})")
check(res["positions"] == 1 and len(os.listdir(os.path.join(store.folder, "positions"))) == 1, "the .bgp positions are opened and kept")
check(os.path.isfile(store.records["bkphoto-0001"]["photo"]), "the photo file exists in the backup PC's photo folder")
res2 = bi.scan_once()
check(res2["new"] == 0 and res2["positions"] == 0 or res2["new"] == 0, "a second pass takes nothing twice")
st = json.load(open(os.path.join(backup_cert, "backup", "intake.json")))
check(st["refused"] == 0 and st["at"] > 0, "intake.json says when it last scanned and how many were refused")

# ---- never_deletes_or_writes_in_drive_folder ----------------------------------------------------------------------------------------------
check(tree(root) == before, "the Drive folder is exactly as it was: nothing deleted, trashed, written or touched")
check(not [f for _r, _d, fs in os.walk(root) for f in fs if not (f.endswith(".bfp") or f.endswith(".bgp"))],
      "no receipt, marker or temporary file appeared in it")

# ---- no_pack_no_intake ---------------------------------------------------------------------------------------------------------------------
check(backup_intake.load_pack(os.path.join(work, "empty-cert"), backup) is None, "no pack file: no pack")
stranger = HandoverKey(os.path.join(work, "stranger"))
check(backup_intake.load_pack(backup_cert, stranger) is None, "a pack that is not for this computer is not opened")
none = backup_intake.BackupIntake([root], PhotoStore(os.path.join(work, "np")), backup, None)
none.start()
check(none.thread is None and none.scan_once() == {"new": 0, "positions": 0, "refused": 0}, "without a pack nothing runs and nothing is taken")

# ---- refused_file_counted_in_health -----------------------------------------------------------------------------------------------------------
junk = os.path.join(ofolder, "p-DEV1", "000099-0123456789ab.bfp")
open(junk, "wb").write(b"junk, not a .bfp")
put_photo("bkphoto-other-room", 3, key=k_gps, folder=ofolder)            # a phone of another code, put in this office's folder
res3 = bi.scan_once()
check(bi.refused() >= 2 and res3["refused"] >= 2, f"refused files are counted ({res3})")
ctx = Ctx(get_setting={"biruni/backup/auto-intake": True}, cert_folder=backup_cert, photos_folder=store.folder, now=time.time())
rows = {r["id"]: r for r in health_rows._check_backup(ctx)}
check(rows.get("backup_intake", {}).get("level") == "warn" and "refused" in rows["backup_intake"]["text"],
      "the health row is amber and says files were refused")
ctx_old = Ctx(get_setting={"biruni/backup/auto-intake": True}, cert_folder=backup_cert, photos_folder=store.folder, now=time.time() + 7200)
check({r["id"]: r for r in health_rows._check_backup(ctx_old)}["backup_intake"]["level"] == "warn", "no scan for an hour is amber")
os.remove(junk)
for f in os.listdir(os.path.join(ofolder, "p-DEV1")):
    if f.startswith("000003"):
        os.remove(os.path.join(ofolder, "p-DEV1", f))
bi2 = backup_intake.BackupIntake([root], PhotoStore(os.path.join(work, "bp2")), backup, pack, state_folder=backup_cert)
bi2.scan_once()
ctx_ok = Ctx(get_setting={"biruni/backup/auto-intake": True}, cert_folder=backup_cert, photos_folder=store.folder, now=time.time())
check({r["id"]: r for r in health_rows._check_backup(ctx_ok)}["backup_intake"]["level"] == "ok", "clean and recent: green")
ctx_off = Ctx(get_setting={}, cert_folder=backup_cert, photos_folder=store.folder, now=time.time())
check("backup_intake" not in {r["id"]: r for r in health_rows._check_backup(ctx_off)}, "setting off: no intake row")

# ---- surveyors_from_the_pack ---------------------------------------------------------------------------------------------------------------------
sk = "srv-secret-xyz"
spack = recipients.open_office_pack(backup, recipients.office_pack(
    backup.public, KEYS, {"qr_mode": "per_surveyor", "surveyors": {"s1": {"id": "s1", "name": "Ali", "secret": sk}}},
    office_pub=office.public))
put_photo("bkphoto-srv-1", 7, sk=sk)
store3 = PhotoStore(os.path.join(work, "bp3"))
check(backup_intake.BackupIntake([root], store3, backup, pack).scan_once()["new"] == 0 or "bkphoto-srv-1" not in store3.records,
      "a per-surveyor photo is refused without that surveyor's code")
store4 = PhotoStore(os.path.join(work, "bp4"))
backup_intake.BackupIntake([root], store4, backup, spack).scan_once()
check("bkphoto-srv-1" in store4.records, "the surveyor codes of the pack (not the backup PC's own list) let it in")

shutil.rmtree(work, ignore_errors=True)
check.done()
