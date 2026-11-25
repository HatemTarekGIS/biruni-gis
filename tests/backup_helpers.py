r"""Shared set-up for the no-loss P9 backup tests (not a test itself): a temp office, photos with a ledger, an engine over
the fake restic, and a way to say "the backup disk is on another volume" (the temp folders all share one)."""
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
FAKE = os.path.join(HERE, "fake_restic.py")

from biruni.backup import engine  # noqa: E402
from biruni.backup.restic import Restic  # noqa: E402
from biruni.field_photos import copies  # noqa: E402

REAL_DIFFERENT_VOLUME = copies.different_volume
# the child is our own python (the engine's), which finds its library through these; a real restic needs none
PY_ENV = {k: v for k, v in os.environ.items() if k.startswith(("PYTHON", "OSGEO4W", "GDAL", "PROJ", "QGIS", "VSI"))}


def fake_restic(repo, pw, cls=Restic):
    return cls([sys.executable, FAKE], repo, pw, env=PY_ENV)


def other_volume(on=True):
    """engine uses copies.different_volume: True makes the temp repository count as 'another disk'."""
    copies.different_volume = (lambda a, b: True) if on else REAL_DIFFERENT_VOLUME


class Env:
    def __init__(self, extra=None, enabled=True, mirror=""):
        self.work = tempfile.mkdtemp(prefix="biruni_p9_")
        self.cert = os.path.join(self.work, "cert")
        self.photos = os.path.join(self.work, "photos")
        self.repo = os.path.join(self.work, "repo")
        os.makedirs(self.cert)
        os.makedirs(self.photos)
        self.settings = {"biruni/backup/enabled": enabled, "biruni/backup/repo": self.repo,
                         "biruni/backup/org-name": "Acme Office"}
        if mirror:
            self.settings["biruni/field-photos/mirror-folder"] = mirror
        self.settings.update(extra or {})
        self.ledger = copies.CopyLedger(self.photos)

    def get(self, k, d=None):
        return self.settings.get(k, d)

    def factory(self, repo, pw):
        return fake_restic(repo, pw)

    def engine(self, factory=None, now=None, **kw):
        if now:
            kw["now"] = now
        return engine.Engine(self.cert, self.photos, self.get, restic_factory=factory or self.factory,
                             ledger=self.ledger, **kw)

    def add_photos(self, n=3, day="2026-11-24", start=0, with_sha=True):
        d = os.path.join(self.photos, day)
        os.makedirs(d, exist_ok=True)
        out = []
        for i in range(start, start + n):
            data = (f"photo {day} {i} ".encode() * 40) + os.urandom(16)
            path = os.path.join(d, f"p{i}.jpg")
            with open(path, "wb") as f:
                f.write(data)
            sha = hashlib.sha256(data).hexdigest()
            iid = f"{day}-{i}"
            with open(os.path.join(self.photos, "photos.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": iid, "photo": path, "sha": sha if with_sha else ""}) + "\n")
            self.ledger.mark(iid, "office", sha=sha if with_sha else "")
            out.append((iid, path, sha))
        return out

    def places(self, iid):
        return (copies.CopyLedger(self.photos).get(iid) or {}).get("places", {})

    def calls(self):
        try:
            return open(os.path.join(self.repo, "calls.log"), encoding="utf-8").read()
        except OSError:
            return ""

    def snap_file(self, path):
        """The repository's copy of a photo (so a test can damage it)."""
        base = os.path.join(self.repo, "snaps")
        rel = os.path.splitdrive(os.path.abspath(path))
        tail = (rel[0].rstrip(":") + rel[1]).replace("\\", "/").strip("/").split("/")
        for sid in sorted(os.listdir(base)):
            p = os.path.join(base, sid, *tail)
            if os.path.isfile(p):
                return p
        return ""


class Checker:
    def __init__(self):
        self.failures = []

    def __call__(self, cond, msg):
        print(("PASS " if cond else "FAIL ") + msg, flush=True)
        if not cond:
            self.failures.append(msg)

    def done(self):
        print("\n" + ("FAILED %d" % len(self.failures) if self.failures else "ALL PASSED"), flush=True)
        sys.exit(1 if self.failures else 0)
