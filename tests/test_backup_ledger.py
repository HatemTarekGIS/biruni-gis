r"""No-loss P9: a photo is marked `backup` in the ledger only when a restic dump's file hash equals the ledger's sha, and
only when the repository is on another volume than the photos and the second-copy folder.

Run:  engine\bin\python-qgis.bat tests\test_backup_ledger.py
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backup_helpers import Checker, Env, fake_restic, other_volume  # noqa: E402
from biruni.backup.restic import Restic  # noqa: E402

check = Checker()

# ---- marks_backup_only_after_dump_hash_matches ----------------------------------------------------------------------------
other_volume(True)
e = Env()
items = e.add_photos(3)
res = e.engine().run_backup()
check(res["status"] == "ok" and res["marked"] == 3, f"all three photos marked after the dump matched ({res})")
check(all(e.places(i).get("backup") for i, _p, _s in items), "the ledger file holds `backup` for each (read back from disk)")
res2 = e.engine().run_backup()
check(res2["marked"] == 0, "a second run has nothing left to mark")
more = e.add_photos(2, start=10)
res3 = e.engine().run_backup()
check(res3["marked"] == 2 and all(e.places(i).get("backup") for i, _p, _s in more), "photos added later are marked by the next run")
no_sha = e.add_photos(1, day="2026-11-25", start=50, with_sha=False)
e.engine().run_backup()
check(not e.places(no_sha[0][0]).get("backup"), "a photo whose ledger has no sha is never marked (nothing to compare)")
shutil.rmtree(e.work, ignore_errors=True)


# ---- corrupt_dump_not_marked --------------------------------------------------------------------------------------------------
class Damaging(Restic):
    """Damages one stored photo right after the snapshot is made (a bad disk)."""
    victim = None
    env = None

    def backup(self, paths, tag="biruni", on_event=None):
        r = super().backup(paths, tag, on_event)
        p = Damaging.env.snap_file(Damaging.victim)
        with open(p, "ab") as f:
            f.write(b"bit rot")
        return r


e = Env()
items = e.add_photos(3)
Damaging.victim, Damaging.env = items[1][1], e
res = e.engine(lambda repo, pw: fake_restic(repo, pw, Damaging)).run_backup()
st = {i: bool(e.places(i).get("backup")) for i, _p, _s in items}
check(res["marked"] == 2 and st[items[0][0]] and st[items[2][0]] and not st[items[1][0]],
      f"the damaged photo is not marked, the others are ({st})")
shutil.rmtree(e.work, ignore_errors=True)

# ---- same_volume_repo_never_marks ---------------------------------------------------------------------------------------------
other_volume(False)          # the real check: the temp repository shares the photos' volume
e = Env()
items = e.add_photos(2)
res = e.engine().run_backup()
check(res["marked"] == 0 and not any(e.places(i).get("backup") for i, _p, _s in items),
      "a repository on the photos' own volume never marks `backup`")
check(res["volume"]["ok"] is False and res["volume"]["why"] == "photos", "… and the result says why")
shutil.rmtree(e.work, ignore_errors=True)

# a repository on the mirror's volume does not count either, even when it is away from the photos
from biruni.field_photos import copies  # noqa: E402

mirror = None
e = Env()
mirror = os.path.join(e.work, "mirror")
os.makedirs(mirror)
e.settings["biruni/field-photos/mirror-folder"] = mirror
copies.different_volume = lambda a, b: os.path.abspath(a) == os.path.abspath(e.photos)    # photos: other; mirror: same
items = e.add_photos(2)
res = e.engine().run_backup()
check(res["marked"] == 0 and res["volume"]["why"] == "mirror", f"a repository on the mirror's volume never marks ({res['volume']})")
shutil.rmtree(e.work, ignore_errors=True)

check.done()
