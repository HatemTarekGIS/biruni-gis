r"""No-loss P9: the pinned tool download (backup/tools.py) with no network: a bad zip hash is refused and leaves nothing, a
tampered program is refused before it is run, placeholder pins refuse to work, pins are official GitHub release addresses.

Run:  engine\bin\python-qgis.bat tests\test_backup_tools.py
"""
import hashlib
import io
import os
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backup_helpers import Checker  # noqa: E402
from biruni.backup import tools  # noqa: E402
from biruni.backup.restic import Restic  # noqa: E402

check = Checker()
work = tempfile.mkdtemp(prefix="biruni_p9_tools_")
os.environ.pop("BIRUNI_TOOLS", None)

EXE = b"MZ not really a program " * 100
MEMBER = "restic_9.9.9_windows_amd64.exe"
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr(MEMBER, EXE)
ZIP = buf.getvalue()


def pins(zip_sha=None, exe_sha=None):
    return {"restic": {"version": "9.9.9", "url": "https://github.com/restic/restic/releases/download/v9.9.9/x.zip",
                       "zip_sha256": zip_sha or hashlib.sha256(ZIP).hexdigest(),
                       "exe_sha256": exe_sha or hashlib.sha256(EXE).hexdigest(), "member": MEMBER}}


class Resp(io.BytesIO):
    length = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(data):
    return lambda url, timeout=60: Resp(data)


def tree(d):
    return [os.path.join(r, f) for r, _d, fs in os.walk(d) for f in fs]


# ---- bad_zip_hash_refused_and_part_removed -----------------------------------------------------------------------------------
td = os.path.join(work, "tools1")
try:
    tools.download("restic", pins=pins(zip_sha="0" * 64), tools_dir=td, opener=opener(ZIP))
    check(False, "a zip with the wrong hash is refused")
except tools.ToolError as x:
    check("zip" in str(x), f"a zip with the wrong hash is refused ({x})")
check(not tree(td), f"nothing is left behind (no .part, no program): {tree(td)}")
try:
    tools.download("restic", pins=pins(exe_sha="1" * 64), tools_dir=td, opener=opener(ZIP))
    check(False, "a program with the wrong hash is refused")
except tools.ToolError:
    check(not tree(td), "a program whose hash is wrong is refused and removed too")
try:
    tools.download("restic", pins=pins(), tools_dir=td, opener=opener(b"not a zip at all"))
except tools.ToolError:
    check(not tree(td), "a download that is not a zip is refused (its hash fails first)")

# the good path, and find() with the same pins
path = tools.download("restic", pins=pins(), tools_dir=td, opener=opener(ZIP))
check(os.path.isfile(path) and open(path, "rb").read() == EXE and len(tree(td)) == 1, "a good download is put in place, alone")
check(tools.find("restic", pins=pins(), tools_dir=td) == path, "find() finds it under <settings>\\tools\\restic\\<version>")
check(tools.find("restic", pins=pins(), tools_dir=os.path.join(work, "empty")) is None, "and gives None where it is not")

# ---- tampered_exe_refused_before_spawn ---------------------------------------------------------------------------------------
with open(path, "ab") as f:
    f.write(b"tampered")
try:
    tools.find("restic", pins=pins(), tools_dir=td)
    check(False, "find() refuses a tampered program")
except tools.ToolError:
    check(True, "find() refuses a tampered program")
r = Restic(path, os.path.join(work, "repo"), "pw", pins=pins())
try:
    r.exists()
    check(False, "a tampered program is not run")
except tools.ToolError:
    check(not os.path.exists(os.path.join(work, "repo")), "a tampered program is refused before it is run (ToolError, not a spawn error)")

# a developer's BIRUNI_TOOLS copy is held to the same pin
dev = os.path.join(work, "dev", "restic")
os.makedirs(dev)
open(os.path.join(dev, MEMBER), "wb").write(EXE + b"x")
os.environ["BIRUNI_TOOLS"] = os.path.join(work, "dev")
try:
    tools.find("restic", pins=pins(), tools_dir=os.path.join(work, "none"))
    check(False, "BIRUNI_TOOLS copies are checked too")
except tools.ToolError:
    check(True, "BIRUNI_TOOLS copies are checked too")
os.environ.pop("BIRUNI_TOOLS")

# ---- placeholders ------------------------------------------------------------------------------------------------------------------
ph = pins(exe_sha=tools.PLACEHOLDER)
for fn, what in ((lambda: tools.find("restic", pins=ph, tools_dir=td), "find"),
                 (lambda: tools.download("restic", pins=ph, tools_dir=td, opener=opener(ZIP)), "download"),
                 (lambda: tools.verified(path, "restic", pins=ph), "verified")):
    try:
        fn()
        check(False, f"{what}() refuses placeholder pins")
    except tools.ToolError:
        check(True, f"{what}() refuses placeholder pins")
check(not tools.is_placeholder(tools.PINNED["restic"]), "the shipped restic pin is real (no placeholder)")

# ---- pinned_urls_are_github_release_https ------------------------------------------------------------------------------------------
for name, ent in tools.PINNED.items():
    u = ent["url"]
    check(u.startswith("https://github.com/") and "/releases/download/" in u and ent["version"] in u and u.endswith(".zip"),
          f"{name}: the pinned address is an https GitHub release download ({u})")
    check(len(ent["zip_sha256"]) == 64 and len(ent["exe_sha256"]) == 64, f"{name}: both hashes are SHA-256")
    check("latest" not in u, f"{name}: never 'latest'")

shutil.rmtree(work, ignore_errors=True)
check.done()
