"""Copy ledger and second-copy mirror for the Biruni no-loss invariant.

Every photo, state item and position batch has a copy ledger entry in `copies.jsonl`:
  {"id": str, "sha": str, "ssha": str, "places": {"office": int, "mirror": int, "drive_mb": int,
   "r2": int, "backup": int, "archive": int}}

The phone may delete an item when `count(id) >= 2` (office + drive_mb counts as 2).
The Drive mailbox may be trashed only when `non_mailbox_count(id) >= 2` (office + mirror or backup/archive).
Single-PC offices configure a "second copy folder" on another volume; every file written there is
re-hashed from disk before being marked `mirror` in the ledger.
"""
import datetime as dt
import hashlib
import json
import os
import threading
import time

LEDGER_NAME = "copies.jsonl"
PLACES = ("office", "mirror", "drive_mb", "r2", "backup", "archive")
NON_MAILBOX_PLACES = ("office", "mirror", "backup", "archive")


def sha256_bytes(data):
    """Hex SHA-256 of bytes."""
    return hashlib.sha256(bytes(data)).hexdigest()


def sha256_file(path):
    """Hex SHA-256 of a file read back from disk."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def volume_id(path):
    """Return a normalized volume identifier for `path` (drive root or volume serial on Windows, st_dev on POSIX)."""
    if not path:
        return ""
    ap = os.path.abspath(str(path))
    if os.name == "nt":
        import ctypes

        drive, _ = os.path.splitdrive(ap)
        root = (drive + "\\") if drive else ap
        serial = ctypes.c_ulong(0)
        try:
            probe = ap if os.path.exists(ap) else root
            if ctypes.windll.kernel32.GetVolumeInformationW(
                root, None, 0, ctypes.byref(serial), None, None, None, 0
            ) and serial.value:
                return f"vol:{serial.value:08x}:{drive.lower()}"
        except Exception:  # noqa: BLE001
            pass
        try:
            st = os.stat(probe)
            if getattr(st, "st_dev", 0):
                return f"dev:{st.st_dev}:{drive.lower()}"
        except OSError:
            pass
        return drive.lower()
    try:
        probe = ap
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        return f"dev:{os.stat(probe).st_dev}"
    except OSError:
        return ""


def different_volume(path_a, path_b):
    """True when `path_a` and `path_b` live on different volumes (or when `path_b` is on a distinct drive/mount)."""
    if not path_a or not path_b:
        return False
    ap_a = os.path.normcase(os.path.abspath(str(path_a)))
    ap_b = os.path.normcase(os.path.abspath(str(path_b)))
    if ap_a == ap_b or ap_b.startswith(ap_a + os.sep) or ap_a.startswith(ap_b + os.sep):
        return False
    da, _ = os.path.splitdrive(ap_a)
    db, _ = os.path.splitdrive(ap_b)
    if da and db and da != db:
        return True
    va, vb = volume_id(ap_a), volume_id(ap_b)
    if va and vb and va != vb:
        return True
    return False


class CopyLedger:
    """Append-only `copies.jsonl` tracking verified copies of each item across independent places."""

    def __init__(self, folder, mirror_folder=""):
        self.folder = folder
        self.mirror_folder = str(mirror_folder or "").strip()
        self.path = os.path.join(folder, LEDGER_NAME)
        self.lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)
        self.records = {}
        self._load()

    def _load(self):
        self.records = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"]:
                        places = r.get("places")
                        places = {k: int(v) for k, v in places.items() if k in PLACES and v} if isinstance(places, dict) else {}
                        prev = self.records.get(r["id"])
                        if prev is not None:
                            merged = dict(prev["places"])
                            merged.update(places)
                            self.records[r["id"]] = {
                                "id": r["id"],
                                "sha": str(r.get("sha") or prev.get("sha") or ""),
                                "ssha": str(r.get("ssha") or prev.get("ssha") or ""),
                                "places": merged,
                            }
                        else:
                            self.records[r["id"]] = {
                                "id": r["id"],
                                "sha": str(r.get("sha") or ""),
                                "ssha": str(r.get("ssha") or ""),
                                "places": places,
                            }
        except OSError:
            pass

    def _append(self, rec):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def mark(self, item_id, place=None, sha="", ssha="", places=()):
        """Record one or more verified places (and sha/ssha) for `item_id` and return the updated record."""
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("bad item id")
        to_mark = []
        if place:
            to_mark.append(place)
        for p in places or ():
            if p and p not in to_mark:
                to_mark.append(p)
        for p in to_mark:
            if p not in PLACES:
                raise ValueError(f"unknown copy place: {p}")
        now_ms = int(time.time() * 1000)
        with self.lock:
            cur = self.records.get(item_id)
            if cur is None:
                cur = {"id": item_id, "sha": "", "ssha": "", "places": {}}
            else:
                cur = {"id": item_id, "sha": cur["sha"], "ssha": cur["ssha"], "places": dict(cur["places"])}
            changed = False
            if sha and cur["sha"] != sha:
                cur["sha"] = str(sha)
                changed = True
            if ssha and cur["ssha"] != ssha:
                cur["ssha"] = str(ssha)
                changed = True
            for p in to_mark:
                if not cur["places"].get(p):
                    cur["places"][p] = now_ms
                    changed = True
            if changed or item_id not in self.records:
                self.records[item_id] = cur
                self._append(cur)
            return dict(cur, places=dict(cur["places"]))

    def get(self, item_id):
        with self.lock:
            r = self.records.get(item_id)
            return dict(r, places=dict(r["places"])) if r else None

    def count(self, item_id):
        """Total verified copies for `item_id` (office + drive_mb counts as 2 for the phone delete rule)."""
        with self.lock:
            r = self.records.get(item_id)
            if not r:
                return 0
            return sum(1 for p in PLACES if r["places"].get(p))

    def non_mailbox_count(self, item_id):
        """Verified non-mailbox copies (office, mirror, backup, archive). Drive mailbox may be trashed at >= 2."""
        with self.lock:
            r = self.records.get(item_id)
            if not r:
                return 0
            return sum(1 for p in NON_MAILBOX_PLACES if r["places"].get(p))

    def receipt_info(self, item_id):
        """Return (count, hash_hex, h8) for `item_id`, preferring `ssha` over `sha`."""
        with self.lock:
            r = self.records.get(item_id)
            if not r:
                return 0, "", ""
            c = sum(1 for p in PLACES if r["places"].get(p))
            h = r.get("ssha") or r.get("sha") or ""
            return c, h, h[:8]

    def set_mirror_folder(self, mirror_folder):
        self.mirror_folder = str(mirror_folder or "").strip()

    def write_mirror_file(self, item_id, src_path, rel_path=None, expected_sha="", ssha=""):
        """Copy `src_path` into `self.mirror_folder`, re-hash the written file from disk, and mark `mirror`."""
        mf = self.mirror_folder
        if not mf or not src_path or not os.path.isfile(src_path):
            return False
        try:
            if not rel_path:
                try:
                    rel = os.path.relpath(os.path.abspath(src_path), os.path.abspath(self.folder))
                    if not rel.startswith(".."):
                        rel_path = rel
                except ValueError:
                    rel_path = None
            if not rel_path:
                day = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
                rel_path = os.path.join(day, os.path.basename(src_path))
            dst = os.path.join(mf, rel_path)
            os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
            part = dst + ".part"
            with open(src_path, "rb") as rf, open(part, "wb") as wf:
                while True:
                    buf = rf.read(1 << 20)
                    if not buf:
                        break
                    wf.write(buf)
                wf.flush()
                os.fsync(wf.fileno())
            os.replace(part, dst)
            want = expected_sha or sha256_file(src_path)
            got = sha256_file(dst)
            if got != want:
                try:
                    os.remove(dst)
                except OSError:
                    pass
                return False
            self.mark(item_id, "mirror", sha=want, ssha=ssha)
            return True
        except OSError:
            return False

    def write_mirror_bytes(self, item_id, rel_path, data, expected_sha="", ssha=""):
        """Write `data` to `mirror_folder/rel_path`, re-hash from disk, and mark `mirror`."""
        mf = self.mirror_folder
        if not mf or not rel_path:
            return False
        raw = bytes(data)
        want = expected_sha or sha256_bytes(raw)
        try:
            dst = os.path.join(mf, rel_path)
            os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
            part = dst + ".part"
            with open(part, "wb") as wf:
                wf.write(raw)
                wf.flush()
                os.fsync(wf.fileno())
            os.replace(part, dst)
            got = sha256_file(dst)
            if got != want:
                try:
                    os.remove(dst)
                except OSError:
                    pass
                return False
            self.mark(item_id, "mirror", sha=want, ssha=ssha)
            return True
        except OSError:
            return False

    def sync_all_to_mirror(self, records=(), state_files=()):
        """Mirror any office photos or state files not yet mirrored into `self.mirror_folder`."""
        mf = self.mirror_folder
        if not mf:
            return 0
        done = 0
        for r in records or ():
            if not isinstance(r, dict) or r.get("preview"):
                continue
            iid = r.get("id")
            p = r.get("photo")
            if not iid or not p or not os.path.isfile(p):
                continue
            cur = self.get(iid)
            if cur and cur["places"].get("mirror"):
                continue
            if self.write_mirror_file(iid, p, expected_sha=r.get("sha") or "", ssha=r.get("ssha") or ""):
                done += 1
        for iid, spath, ssha in state_files or ():
            if not iid or not spath or not os.path.isfile(spath):
                continue
            cur = self.get(iid)
            if cur and cur["places"].get("mirror"):
                continue
            if self.write_mirror_file(iid, spath, ssha=ssha):
                done += 1
        return done
