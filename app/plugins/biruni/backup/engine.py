"""What a backup run does, end to end (no Qt; the scheduler and the dialog call these from a background thread).

Rules that matter:
- Only a restic dump whose file hashes equal the ledger's `sha` marks a photo `backup`, and only when the repository is
  on a different volume from the photo folder AND from the second-copy (mirror) folder. A synced folder (Syncthing)
  never marks `backup`.
- Biruni never schedules forget or prune. admin_prune is the only path that prunes, and it needs the organisation's
  name typed in full.
- state.json (atomic) holds the last result of each job for the health rows; log.jsonl holds one line per job and
  never a secret.
"""
import hashlib
import json
import os
import random
import shutil
import tarfile
import tempfile
import threading
import time

from ..field_photos import copies
from . import SETTINGS, secret
from .restic import PARTIAL, WRONG_PASSWORD, Restic, ResticError

PHOTOS_SETTING = "biruni/field-photos/folder"
MIRROR_SETTING = "biruni/field-photos/mirror-folder"
CHECK_PARTS = 13
DEFAULTS = {"enabled": False, "repo": "", "kind": "usb", "extra-paths": "", "backup-hours": 24, "check-days": 7,
            "restore-days": 30, "vault-days": 7, "auto-intake": False, "org-name": ""}


# ---- settings -------------------------------------------------------------------------------------------------------
def setting(get, key, default=None):
    """One backup setting (``get(full key, default)``; ``get`` may also be a dict)."""
    full = SETTINGS + key
    dflt = DEFAULTS.get(key) if default is None else default
    try:
        v = get.get(full, dflt) if isinstance(get, dict) else get(full, dflt)
    except TypeError:
        v = get(full)
    return dflt if v is None else v


def setting_keys():
    """Every setting a job reads (the scheduler copies them on the main thread)."""
    return [SETTINGS + k for k in DEFAULTS] + [PHOTOS_SETTING, MIRROR_SETTING, "biruni/seal/office"]


def as_bool(v):
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


def as_float(v, d):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(d)


def extra_paths(get):
    raw = setting(get, "extra-paths", "")
    if isinstance(raw, (list, tuple)):
        return [str(p) for p in raw if str(p).strip()]
    raw = str(raw or "").strip()
    if raw.startswith("["):
        try:
            return [str(p) for p in json.loads(raw) if str(p).strip()]
        except ValueError:
            return []
    return [p.strip() for p in raw.split(";") if p.strip()]


def config(get):
    """The settings the scheduler and the health rows use, with their defaults."""
    return {
        "enabled": as_bool(setting(get, "enabled")),
        "repo": str(setting(get, "repo") or "").strip(),
        "kind": str(setting(get, "kind") or "usb"),
        "backup-hours": as_float(setting(get, "backup-hours"), 24),
        "check-days": as_float(setting(get, "check-days"), 7),
        "restore-days": as_float(setting(get, "restore-days"), 30),
        "vault-days": as_float(setting(get, "vault-days"), 7),
        "auto-intake": as_bool(setting(get, "auto-intake")),
    }


# ---- state and log ---------------------------------------------------------------------------------------------------
def backup_dir(cert_folder):
    return os.path.join(cert_folder, "backup")


def load_state(cert_folder):
    try:
        with open(os.path.join(backup_dir(cert_folder), "state.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(cert_folder, state):
    d = backup_dir(cert_folder)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "state.json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(p + ".tmp", p)


def append_log(cert_folder, kind, **fields):
    """One line per job. Never put a password or a key in ``fields``."""
    d = backup_dir(cert_folder)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(fields, kind=kind, at=time.time()), ensure_ascii=False, default=str) + "\n")


def target_present(repo):
    """False when the disk is not there (an unplugged USB disk, a NAS that is off): the run is deferred, not failed.
    The folder itself, or its parent, must exist (a new repository may make its own folder)."""
    repo = str(repo or "")
    if not repo:
        return False
    parent = os.path.dirname(os.path.normpath(repo))
    return os.path.isdir(repo) or os.path.isdir(parent) or (bool(parent) and os.path.isdir(os.path.dirname(parent)))


def snapshot_path(p):
    """Where restic keeps a Windows path inside a snapshot: C:\\a\\b -> /C/a/b."""
    p = os.path.abspath(str(p))
    drive, rest = os.path.splitdrive(p)
    rest = rest.replace("\\", "/")
    if drive[:2].endswith(":"):
        return "/" + drive[0] + rest
    if drive.startswith("\\\\"):
        return "/" + drive.strip("\\").replace("\\", "/") + rest
    return rest if rest.startswith("/") else "/" + rest


def _sha_stream(f):
    h = hashlib.sha256()
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


# ---- the engine -------------------------------------------------------------------------------------------------------
class Engine:
    """``get_setting(key, default)``: QgsSettings.value or a dict's get. ``restic_factory(repo, password)``: a Restic
    (default: the pinned program). ``ledger``: the live CopyLedger, or None to open the photo folder's own."""

    def __init__(self, cert_folder, photos_folder, get_setting, restic_factory=None, ledger=None, archive_root="",
                 vault_root="", vault_factory=None, now=time.time):
        self.cert, self.photos = cert_folder, photos_folder
        self.get = get_setting
        self._factory = restic_factory
        self._ledger = ledger
        self.archive_root, self.vault_root, self.vault_factory = archive_root, vault_root, vault_factory
        self.now = now
        self.lock = threading.Lock()      # one job at a time

    # ---- pieces -----------------------------------------------------------------------------------------------------
    @property
    def repo(self):
        return str(setting(self.get, "repo") or "").strip()

    def mirror(self):
        return str(self.get(MIRROR_SETTING, "") or "").strip() if callable(self.get) else ""

    def ledger(self):
        if self._ledger is not None:
            return self._ledger
        return copies.CopyLedger(self.photos, mirror_folder=self.mirror())

    def restic(self):
        pw, _created = secret.ensure_password(self.cert)
        if self._factory is not None:
            return self._factory(self.repo, pw)
        from . import tools

        exe = tools.find("restic")
        if exe is None:
            raise tools.ToolError("restic is not installed on this computer yet")
        return Restic(exe, self.repo, pw)

    def sources(self):
        out = []
        for p in (self.photos, self.archive_root, self.vault_root, os.path.join(self.cert, "keybackup")):
            if p and os.path.exists(p):
                out.append(p)
        out += [p for p in extra_paths(self.get) if os.path.exists(p)]
        return list(dict.fromkeys(os.path.normpath(p) for p in out))

    def volumes_ok(self):
        """(ok, why): the repository is on another volume than the photos and than the second-copy folder."""
        if not copies.different_volume(self.photos, self.repo):
            return False, "photos"
        mf = self.mirror()
        if mf and not copies.different_volume(mf, self.repo):
            return False, "mirror"
        return True, ""

    def photo_items(self):
        """[(id, photo path, ledger sha)] of the photos of this folder that are whole originals."""
        from ..field_photos.server import read_log

        led = self.ledger()
        out = []
        for r in read_log(self.photos):
            if r.get("preview") or not r.get("photo"):
                continue
            cur = led.get(r["id"]) or {}
            out.append((r["id"], r["photo"], str(cur.get("sha") or r.get("sha") or ""), bool((cur.get("places") or {}).get("backup"))))
        return out

    def _record(self, key, value):
        st = load_state(self.cert)
        st[key] = value
        save_state(self.cert, st)
        return st

    def _fail(self, kind, e):
        code = getattr(e, "code", None)
        st = load_state(self.cert)
        st["last_error_code"] = code
        if code == WRONG_PASSWORD:
            st["password_bad"] = True
        save_state(self.cert, st)
        append_log(self.cert, kind, ok=False, code=code, error=str(e)[:300])
        return {"status": "error", "code": code, "error": str(e)}

    # ---- jobs -------------------------------------------------------------------------------------------------------
    def run_backup(self, on_event=None):
        with self.lock:
            return self._run_backup(on_event)

    def _run_backup(self, on_event):
        if not self.repo:
            return {"status": "not-configured"}
        if not target_present(self.repo):
            self._record("last_deferred", {"at": self.now(), "reason": "target absent"})
            append_log(self.cert, "backup", ok=None, deferred=True)
            return {"status": "deferred", "reason": "target absent"}
        try:
            r = self.restic()
            if not r.exists():
                r.init()
            res = r.backup(self.sources(), "biruni", on_event=on_event)
            same_vol = {}
            ok, why = self.volumes_ok()
            marked = 0
            if res["snapshot"] and ok:
                marked = self.mark_from_dump(r, res["snapshot"])
            same_vol = {"ok": ok, "why": why}
        except (ResticError, OSError) as e:
            return self._fail("backup", e)
        except Exception as e:  # noqa: BLE001 - ToolError and the like
            return self._fail("backup", e)
        st = load_state(self.cert)
        st["repo_init"] = True
        st["password_bad"] = False
        st["last_error_code"] = None
        st.setdefault("first_backup", self.now())
        st["last_backup"] = {"at": self.now(), "code": res["code"], "snapshot": res["snapshot"],
                             "unreadable": res["unreadable"][:50], "new": res["new"], "marked": marked}
        st["volume"] = same_vol
        save_state(self.cert, st)
        append_log(self.cert, "backup", ok=res["code"] in (0, PARTIAL), code=res["code"], snapshot=res["snapshot"],
                   unreadable=len(res["unreadable"]), marked=marked, volume_ok=same_vol.get("ok"))
        return {"status": "ok" if res["code"] == 0 else "partial", "code": res["code"], "snapshot": res["snapshot"],
                "unreadable": res["unreadable"], "marked": marked, "volume": same_vol}

    def mark_from_dump(self, r, snap):
        """Dump each day folder of the snapshot that holds photos not yet marked, hash every file as a stream, and
        mark `backup` where the hash is the ledger's sha. Returns how many were marked."""
        led = self.ledger()
        by_dir = {}
        for iid, path, sha, done in self.photo_items():
            if sha and not done:
                by_dir.setdefault(os.path.dirname(os.path.abspath(path)), []).append((iid, os.path.basename(path), sha))
        marked = 0
        for folder, items in by_dir.items():
            try:
                hashes = self._dump_hashes(r, snap, folder)
            except ResticError:
                continue                # this folder is not in the snapshot (made after it): the next run
            for iid, name, sha in items:
                if hashes.get(name) == sha:
                    led.mark(iid, "backup", sha=sha)
                    marked += 1
        return marked

    @staticmethod
    def _dump_hashes(r, snap, folder):
        out = {}
        with r.dump_tar(snap, snapshot_path(folder)) as f:
            with tarfile.open(fileobj=f, mode="r|") as tf:
                for m in tf:
                    if not m.isfile():
                        continue
                    ef = tf.extractfile(m)
                    h = _sha_stream(ef) if ef is not None else ""
                    key = m.name.replace("\\", "/").rsplit("/", 1)[-1]
                    out[key] = None if key in out and out[key] != h else h    # the same name twice: ambiguous
        return out

    def run_check(self):
        with self.lock:
            if not self.repo or not target_present(self.repo):
                return {"status": "deferred"}
            k = int(self.now() // (7 * 86400)) % CHECK_PARTS + 1
            subset = f"{k}/{CHECK_PARTS}"
            try:
                ok, detail = self.restic().check(subset)
            except Exception as e:  # noqa: BLE001
                return self._fail("check", e)
            self._record("last_check", {"at": self.now(), "ok": ok, "detail": detail[:300], "subset": subset})
            append_log(self.cert, "check", ok=ok, subset=subset)
            return {"status": "ok" if ok else "failed", "subset": subset, "detail": detail}

    def run_restore_test(self, rng=random):
        """Restore one random day folder (only photos already marked `backup`) from the latest snapshot into a
        temporary folder with --verify, compare each file with the ledger and the live file, delete the temp folder."""
        with self.lock:
            if not self.repo or not target_present(self.repo):
                return {"status": "deferred"}
            led = self.ledger()
            by_dir = {}
            for iid, path, sha, done in self.photo_items():
                if done:
                    by_dir.setdefault(os.path.dirname(os.path.abspath(path)), []).append((iid, path, sha))
            if not by_dir:
                return {"status": "nothing", "detail": "no photo is marked backup yet"}
            folder = rng.choice(sorted(by_dir))
            tmp = tempfile.mkdtemp(prefix="restore-", dir=self._tmp_root())
            try:
                r = self.restic()
                snaps = sorted(r.snapshots(), key=lambda s: str(s.get("time") or ""))
                if not snaps:
                    return {"status": "nothing", "detail": "no snapshot"}
                snap = str(snaps[-1].get("id") or snaps[-1].get("short_id") or "latest")
                r.restore(snap, snapshot_path(folder), tmp)
                bad = []
                for iid, path, sha in by_dir[folder]:
                    got = os.path.join(tmp, *snapshot_path(path).strip("/").split("/"))
                    try:
                        with open(got, "rb") as f:
                            h = _sha_stream(f)
                    except OSError:
                        bad.append(iid)
                        continue
                    live = ""
                    if os.path.isfile(path):
                        try:
                            live = copies.sha256_file(path)
                        except OSError:
                            live = ""
                    if (sha and h != sha) or (live and h != live):
                        bad.append(iid)
                res = {"at": self.now(), "ok": not bad, "checked": len(by_dir[folder]), "mismatches": bad[:50],
                       "folder": os.path.basename(folder)}
            except Exception as e:  # noqa: BLE001
                return self._fail("restore", e)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            self._record("last_restore", res)
            append_log(self.cert, "restore", ok=res["ok"], checked=res["checked"], mismatches=len(res["mismatches"]))
            return dict(res, status="ok" if res["ok"] else "failed")

    def _tmp_root(self):
        d = os.path.join(backup_dir(self.cert), "restore-tmp")
        os.makedirs(d, exist_ok=True)
        return d

    def run_vault_snapshot(self):
        """Keys and lists only (never photos): the key backups, the recipients list, the surveyor codes. The shards wait
        in the vault's outbox until a holder takes them; the health row nags while any are unplaced."""
        with self.lock:
            paths = [p for p in (os.path.join(self.cert, "keybackup"), os.path.join(self.cert, "recipients.json"),
                                 os.path.join(self.photos, "surveyor_keys.json")) if os.path.exists(p)]
            if not paths:
                return {"status": "nothing"}
            try:
                if self.vault_factory is not None:
                    v = self.vault_factory()
                else:
                    from ..team_vault.core import Vault

                    v = Vault(self.vault_root or None)
                v.snapshot(paths, label="biruni-auto")
                unplaced = sum(len(s) for s in v.outbox().values())
            except Exception as e:  # noqa: BLE001
                return self._fail("vault", e)
            self._record("last_vault", {"at": self.now(), "unplaced": unplaced})
            append_log(self.cert, "vault", ok=True, unplaced=unplaced)
            return {"status": "ok", "unplaced": unplaced}

    # ---- the only way to prune --------------------------------------------------------------------------------------
    def org_name(self):
        n = str(setting(self.get, "org-name") or "").strip()
        if n:
            return n
        try:
            v = self.get("biruni/seal/office", "") if callable(self.get) else ""
            return str(v or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def admin_prune(self, policy, typed_confirmation):
        """forget + prune with ``policy`` ({"keep-daily": 30, ...}); PermissionError unless ``typed_confirmation`` is
        the organisation's name exactly. Nothing else in Biruni calls forget."""
        org = self.org_name()
        if not org or str(typed_confirmation or "").strip() != org:
            raise PermissionError("type the organisation's name to prune the backup")
        if not policy:
            raise ValueError("a keep policy is needed")
        with self.lock:
            self.restic().forget_prune(policy)
            append_log(self.cert, "prune", ok=True, policy=policy)
        return True
