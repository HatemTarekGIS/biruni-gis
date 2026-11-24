r"""No-loss P9: the office's one restic password (DPAPI file, backup/secret.py) travels in the v2 key backup
(body["backup_pw"]); a restore keeps a differing password as .old; a v2 backup made before this still restores.

Run:  engine\bin\python-qgis.bat tests\test_backup_keys.py
"""
import glob
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backup_helpers import Checker  # noqa: E402
from biruni.backup import secret  # noqa: E402
from biruni.field_photos import bridge, handover  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402
from biruni.field_photos.tunnel import unprotect  # noqa: E402

check = Checker()
work = tempfile.mkdtemp(prefix="biruni_p9_keys_")


class FakeSettings:
    def __init__(self):
        self.d = {}

    def value(self, k, default=None):
        return self.d.get(k, default)

    def setValue(self, k, v):
        self.d[k] = v


STRONG = "Field-Office 2026 keys!"

# ---- password_created_once_dpapi --------------------------------------------------------------------------------------------------
cert = os.path.join(work, "cert")
check(secret.load_password(cert) == "", "no password until one is made")
pw, created = secret.ensure_password(cert)
pw2, created2 = secret.ensure_password(cert)
check(created and not created2 and pw == pw2 and len(pw) >= 24, "made once; the second call returns the same one")
raw = open(secret.path(cert), encoding="ascii").read()
check(pw not in raw and unprotect(raw.strip()) == pw, "the file is DPAPI-protected, not plain text")
open(secret.path(cert), "w", encoding="ascii").write("garbage that cannot be unprotected")
pw3, created3 = secret.ensure_password(cert)
check(created3 and pw3 != pw and glob.glob(secret.path(cert) + ".unreadable-*"), "an unreadable file is moved aside, never written over")

# ---- backup_v2_roundtrip_carries_backup_pw ---------------------------------------------------------------------------------------------
st = FakeSettings()
bridge.load_key(st)
office = os.path.join(work, "office")
HandoverKey(office)
opw, _ = secret.ensure_password(office)
bak = os.path.join(work, "office.biruni-key")
handover.backup_v2(office, bak, STRONG, st)
check(opw not in open(bak, encoding="utf-8").read(), "the password is sealed inside the backup, not readable in it")
check(glob.glob(os.path.join(office, "keybackup", "office-*.biruni-key")), "a copy of the backup is kept in <cert>\\keybackup\\")
new = os.path.join(work, "new-pc")
handover.restore(new, bak, STRONG, FakeSettings())
check(secret.load_password(new) == opw, "restored on a new PC: the same backup password")

# ---- restore_keeps_differing_pw_as_old --------------------------------------------------------------------------------------------------
other = os.path.join(work, "other-pc")
os.makedirs(other)
mine, _ = secret.ensure_password(other)
check(mine != opw, "(this PC has its own, different password)")
handover.restore(other, bak, STRONG, FakeSettings())
olds = glob.glob(secret.path(other) + ".*.old")
check(secret.load_password(other) == opw and len(olds) == 1 and unprotect(open(olds[0], encoding="ascii").read().strip()) == mine,
      "the backup's password is now in use and the other one is kept as .old")
handover.restore(other, bak, STRONG, FakeSettings())
check(len(glob.glob(secret.path(other) + ".*.old")) == 1, "restoring the same backup again changes nothing")

# ---- v2_without_backup_pw_still_restores --------------------------------------------------------------------------------------------------
old_office = os.path.join(work, "old-office")
HandoverKey(old_office)
st2 = FakeSettings()
bridge.load_key(st2)
bak2 = os.path.join(work, "old.biruni-key")
handover.backup_v2(old_office, bak2, STRONG, st2)
body_has = "backup_pw" in json.dumps(handover.backup_info(bak2)["_doc"])
check(not body_has and not os.path.exists(secret.path(old_office)), "a v2 backup made where no backup password exists has none")
target = os.path.join(work, "old-target")
check(handover.restore(target, bak2, STRONG, FakeSettings()) and secret.load_password(target) == "",
      "it still restores, and leaves no backup password")
mine2, _ = secret.ensure_password(target)
handover.restore(target, bak2, STRONG, FakeSettings())
check(secret.load_password(target) == mine2, "restoring it over a PC that has a password does not touch the password")

shutil.rmtree(work, ignore_errors=True)
check.done()
