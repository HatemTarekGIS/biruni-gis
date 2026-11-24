"""Google Drive archive for Biruni capture jobs (Phase P8, decision 11).

An office option (off by default): after the P7 office archive of a job is built, also copy it into a folder
that Google Drive for desktop syncs, so Google carries it to the cloud.  Nothing here talks to Google: the
upload is "write into the synced folder", and a job counts as archived only after the written copy was
re-hashed from disk.

Formats (per office):
  - ``folder``: the open P7 layout under ``<target>\\<job folder>\\``;
  - ``zip``: one ``<job code>.zip`` per job;
  - ``aes``: one ``<job code>.encrypted.zip`` per job, WinZip AES-256 (AE-2), readable by 7-Zip and WinRAR.
    AES ZIP does not hide file names, so entries use job codes only (flat, no Arabic folder names).

The AES password is generated (strong random), shown once to the office, and stored DPAPI-protected through
``tunnel.protect`` in the cert folder (``drive_archive_pw.dat``).  P9 adds that file to the key backup.

Compression levels for the photos: ``original`` | ``high`` (JPEG q85, long side 4000 px) |
``compact`` (q70, long side 2560 px).  EXIF is kept (Pillow when importable, otherwise Qt QImage with the
original EXIF segment spliced back).  A re-encode that would not shrink the file keeps the original bytes.

Marking: the CopyLedger place ``gdrive`` and ``drive_archive.json`` (job state) are written only after the
written file was re-hashed; a failed or truncated write is never marked.
"""
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import struct
import sys
import tempfile
import threading
import time
import zlib

from qgis.core import QgsApplication, QgsSettings

from ..i18n import tr
from .archive import safe_code_token, safe_win_name
from .copies import CopyLedger, sha256_file

KEY = "biruni/field-photos/"
K_ENABLED = KEY + "drive-archive"
K_TARGET = KEY + "drive-archive-target"
K_FORMAT = KEY + "drive-archive-format"
K_LEVEL = KEY + "drive-archive-level"

FORMATS = ("folder", "zip", "aes")
LEVELS = ("original", "high", "compact")
LEVEL_SPEC = {"original": None, "high": (85, 4000), "compact": (70, 2560)}
PLACE = "gdrive"
STATE_NAME = "drive_archive.json"
PASSWORD_FILE = "drive_archive_pw.dat"
LOW_SPACE_PCT = 10.0
TMP_SUFFIX = ".biruni-tmp"

_STATE_LOCK = threading.Lock()
_THREADS = []


class DriveArchiveError(Exception):
    pass


class WrongPassword(DriveArchiveError):
    pass


# ---- settings ------------------------------------------------------------------------------------------------------
def get_config():
    s = QgsSettings()
    fmt = str(s.value(K_FORMAT, "folder") or "folder")
    level = str(s.value(K_LEVEL, "original") or "original")
    en = s.value(K_ENABLED, False)
    return {
        "enabled": str(en).lower() in ("1", "true", "yes"),
        "target": str(s.value(K_TARGET, "") or "").strip(),
        "format": fmt if fmt in FORMATS else "folder",
        "level": level if level in LEVELS else "original",
    }


def set_config(enabled=None, target=None, fmt=None, level=None):
    s = QgsSettings()
    if enabled is not None:
        s.setValue(K_ENABLED, bool(enabled))
    if target is not None:
        s.setValue(K_TARGET, str(target or "").strip())
    if fmt is not None:
        if fmt not in FORMATS:
            raise ValueError("bad format")
        s.setValue(K_FORMAT, fmt)
    if level is not None:
        if level not in LEVELS:
            raise ValueError("bad level")
        s.setValue(K_LEVEL, level)
    return get_config()


# ---- password ------------------------------------------------------------------------------------------------------
def cert_folder():
    return os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")


def generate_password():
    """A strong random password (about 150 bits), grouped for reading aloud: XXXXX-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(6)]
    return "-".join(groups)


def load_password(folder=None):
    from .tunnel import unprotect

    path = os.path.join(folder or cert_folder(), PASSWORD_FILE)
    try:
        with open(path, encoding="ascii") as f:
            return unprotect(f.read().strip())
    except OSError:
        return ""


def ensure_password(folder=None):
    """Return ``(password, created)``; creates and stores a new one (DPAPI) only when none exists yet.

    The caller shows the password once to the office when ``created`` is true.
    """
    from .tunnel import protect

    folder = folder or cert_folder()
    cur = load_password(folder)
    if cur:
        return cur, False
    pw = generate_password()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, PASSWORD_FILE)
    tmp = path + TMP_SUFFIX
    with open(tmp, "w", encoding="ascii") as f:
        f.write(protect(pw))
    os.replace(tmp, path)
    return pw, True


# ---- WinZip AES-256 (AE-2) -----------------------------------------------------------------------------------------
_SALT_LEN = 16
_KEY_LEN = 32
_PBKDF2_ITER = 1000
_AES_EXTRA_ID = 0x9901
_CHUNK_BLOCKS = 1 << 16   # 1 MiB of keystream at a time


def _derive(password, salt):
    km = hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), salt, _PBKDF2_ITER, 2 * _KEY_LEN + 2)
    return km[:_KEY_LEN], km[_KEY_LEN:2 * _KEY_LEN], km[2 * _KEY_LEN:]


def _ctr_xor(key, data):
    """AES-256-CTR with WinZip's counter: 128-bit little-endian, first block uses 1."""
    from array import array

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    out = bytearray()
    counter = 1
    pos = 0
    n = len(data)
    while pos < n:
        piece = data[pos:pos + _CHUNK_BLOCKS * 16]
        blocks = (len(piece) + 15) // 16
        buf = bytearray(blocks * 16)
        arr = array("Q", range(counter, counter + blocks))
        if sys.byteorder != "little":
            arr.byteswap()
        memoryview(buf).cast("Q")[0::2] = arr
        ks = enc.update(bytes(buf))[:len(piece)]
        out += (int.from_bytes(piece, "little") ^ int.from_bytes(ks, "little")).to_bytes(len(piece), "little")
        counter += blocks
        pos += len(piece)
    return bytes(out)


def _aes_encrypt(password, plain):
    salt = secrets.token_bytes(_SALT_LEN)
    ekey, akey, ver = _derive(password, salt)
    ct = _ctr_xor(ekey, plain)
    mac = hmac.new(akey, ct, hashlib.sha1).digest()[:10]
    return salt + ver + ct + mac


def _aes_decrypt(password, blob):
    if len(blob) < _SALT_LEN + 2 + 10:
        raise DriveArchiveError("truncated AES entry")
    salt, ver, rest = blob[:_SALT_LEN], blob[_SALT_LEN:_SALT_LEN + 2], blob[_SALT_LEN + 2:]
    ct, mac = rest[:-10], rest[-10:]
    ekey, akey, want_ver = _derive(password, salt)
    if not hmac.compare_digest(ver, want_ver):
        raise WrongPassword("wrong password")
    if not hmac.compare_digest(hmac.new(akey, ct, hashlib.sha1).digest()[:10], mac):
        raise DriveArchiveError("authentication code mismatch (damaged data)")
    return _ctr_xor(ekey, ct)


def _dos_time(ts=None):
    t = time.localtime(ts or time.time())
    d = ((max(t.tm_year, 1980) - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    tm = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return tm, d


def write_aes_zip(path, entries, password):
    """Write `entries` ([(name, bytes)]) into `path` as a WinZip AES-256 AE-2 archive (entries stored, then encrypted)."""
    if not password:
        raise DriveArchiveError("empty password")
    tm, dt_ = _dos_time()
    central = []
    with open(path, "wb") as f:
        for name, data in entries:
            nb = name.replace("\\", "/").encode("utf-8")
            blob = _aes_encrypt(password, bytes(data))
            extra = struct.pack("<HHH2sBH", _AES_EXTRA_ID, 7, 2, b"AE", 3, 0)   # AE-2, AES-256, stored
            offset = f.tell()
            f.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 51, 0x0801, 99, tm, dt_, 0, len(blob), len(data),
                                len(nb), len(extra)))
            f.write(nb)
            f.write(extra)
            f.write(blob)
            central.append((nb, extra, len(blob), len(data), offset))
        cd_start = f.tell()
        for nb, extra, csize, usize, offset in central:
            f.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 51, 51, 0x0801, 99, tm, dt_, 0, csize, usize,
                                len(nb), len(extra), 0, 0, 0, 0, offset))
            f.write(nb)
            f.write(extra)
        cd_size = f.tell() - cd_start
        f.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central), cd_size, cd_start, 0))
        f.flush()
        os.fsync(f.fileno())


def read_aes_zip(path, password):
    """Read back an AES ZIP written by `write_aes_zip` (or any AE-1/AE-2 stored/deflated one): ``{name: bytes}``."""
    import zipfile

    out = {}
    with zipfile.ZipFile(path) as zf, open(path, "rb") as f:
        for info in zf.infolist():
            f.seek(info.header_offset)
            hdr = f.read(30)
            if len(hdr) < 30 or struct.unpack("<I", hdr[:4])[0] != 0x04034B50:
                raise DriveArchiveError("bad local header")
            nlen, elen = struct.unpack("<HH", hdr[26:30])
            f.seek(info.header_offset + 30 + nlen)
            extra = f.read(elen)
            blob = f.read(info.compress_size)
            if len(blob) != info.compress_size:
                raise DriveArchiveError("truncated entry")
            if info.compress_type != 99:
                out[info.filename] = blob
                continue
            method = 0
            i = 0
            while i + 4 <= len(extra):
                eid, esz = struct.unpack("<HH", extra[i:i + 4])
                if eid == _AES_EXTRA_ID and esz >= 7:
                    method = struct.unpack("<H", extra[i + 9:i + 11])[0]
                i += 4 + esz
            plain = _aes_decrypt(password, blob)
            if method == 8:
                plain = zlib.decompress(plain, -15)
            out[info.filename] = plain
    return out


# ---- photo compression ---------------------------------------------------------------------------------------------
def _pillow():
    try:
        from PIL import Image  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


_HAVE_PIL = _pillow()


def _exif_segment(jpeg):
    """The APP1 Exif segment (marker included) of a JPEG, or b""."""
    if jpeg[:2] != b"\xff\xd8":
        return b""
    i = 2
    while i + 4 <= len(jpeg) and jpeg[i] == 0xFF:
        marker = jpeg[i + 1]
        if marker in (0xDA, 0xD9):
            break
        seglen = struct.unpack(">H", jpeg[i + 2:i + 4])[0]
        if marker == 0xE1 and jpeg[i + 4:i + 10] == b"Exif\x00\x00":
            return jpeg[i:i + 2 + seglen]
        i += 2 + seglen
    return b""


def _with_exif(jpeg, segment):
    if not segment or jpeg[:2] != b"\xff\xd8":
        return jpeg
    i = 2
    # drop an APP1 Exif the encoder may have written, then insert ours right after SOI (and after JFIF APP0)
    if jpeg[i:i + 2] == b"\xff\xe0":
        i += 2 + struct.unpack(">H", jpeg[i + 2:i + 4])[0]
    return jpeg[:i] + segment + jpeg[i:]


def _reencode_pil(data, quality, max_px):
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    exif = im.info.get("exif") or b""
    im.load()
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    if max(im.size) > max_px:
        im.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    kw = {"quality": quality, "optimize": True}
    if exif:
        kw["exif"] = exif
    im.save(buf, "JPEG", **kw)
    return buf.getvalue()


def _reencode_qt(data, quality, max_px):
    from qgis.PyQt.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from qgis.PyQt.QtGui import QImage

    img = QImage()
    if not img.loadFromData(data):
        raise DriveArchiveError("cannot decode image")
    if max(img.width(), img.height()) > max_px:
        img = img.scaled(max_px, max_px, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPG", quality)
    buf.close()
    return _with_exif(bytes(ba), _exif_segment(data))


def compress_jpeg(data, level="original"):
    """Re-encode JPEG `data` for `level` (EXIF kept).  Never returns something larger than the input."""
    spec = LEVEL_SPEC.get(level)
    if not spec or data[:2] != b"\xff\xd8":
        return data
    quality, max_px = spec
    try:
        out = _reencode_pil(data, quality, max_px) if _HAVE_PIL else _reencode_qt(data, quality, max_px)
    except Exception:  # noqa: BLE001
        try:
            out = _reencode_qt(data, quality, max_px)
        except Exception:  # noqa: BLE001
            return data
    return out if len(out) < len(data) else data


# ---- quota ---------------------------------------------------------------------------------------------------------
def quota_status(target):
    """Free space of the volume holding `target`: ``{total, free, free_pct, warn}`` (``None`` values when unknown)."""
    probe = os.path.abspath(str(target or "."))
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        u = shutil.disk_usage(probe)
    except OSError:
        return {"total": None, "free": None, "free_pct": None, "warn": False}
    pct = (u.free * 100.0 / u.total) if u.total else None
    return {"total": u.total, "free": u.free, "free_pct": pct, "warn": pct is not None and pct < LOW_SPACE_PCT}


def quota_message(q):
    """One line for the office: free space and, below 10%, the warning."""
    if not q or q.get("free") is None:
        return tr("Free space of the Drive volume is unknown.")
    gb = q["free"] / float(1 << 30)
    msg = tr("Free space on the Drive volume: {gb:.1f} GB ({pct:.0f}%).").format(gb=gb, pct=q["free_pct"])
    if q.get("warn"):
        msg += " " + tr("Warning: less than 10% is free. Free some space or upgrade your Google storage.")
    return msg


GOOGLE_STORAGE_URL = "https://one.google.com/storage"


# ---- state ---------------------------------------------------------------------------------------------------------
def _state_path(store_folder):
    return os.path.join(store_folder, STATE_NAME)


def load_state(store_folder):
    try:
        with open(_state_path(store_folder), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("jobs"), dict) else {"jobs": {}}
    except (OSError, ValueError):
        return {"jobs": {}}


def _save_job_state(store_folder, job_id, rec):
    with _STATE_LOCK:
        d = load_state(store_folder)
        d["jobs"][job_id] = rec
        path = _state_path(store_folder)
        tmp = path + TMP_SUFFIX
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def job_archived_to_drive(store_folder, job_id):
    """True when the job was archived to Drive (and verified) at least once."""
    return bool((load_state(store_folder)["jobs"].get(job_id) or {}).get("archived"))


# ---- writing -------------------------------------------------------------------------------------------------------
def _write_atomic(src, dst):
    """Copy `src` into `dst` through a temporary name in the same folder, then rename (never a half file under `dst`)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + TMP_SUFFIX
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _put_verified(src, dst, expected_sha):
    """Write and re-hash the written file; delete it and return False when it does not match."""
    try:
        _write_atomic(src, dst)
        ok = os.path.isfile(dst) and sha256_file(dst) == expected_sha
    except OSError:
        ok = False
    if not ok:
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except OSError:
            pass
    return ok


def _unique(name, used):
    base, ext = os.path.splitext(name)
    cand, n = name, 2
    while cand.lower() in used:
        cand = f"{base}_{n}{ext}"
        n += 1
    used.add(cand.lower())
    return cand


def archive_job_to_drive(store, job_id, build_result, target=None, fmt=None, level=None, ledger=None, password=None):
    """Copy the P7 archive of `job_id` (the dictionary `build_job_archive` returned) into the Drive folder.

    Returns a dictionary: ``ok`` (everything written and verified), ``archived``, ``format``, ``level``,
    ``written`` / ``reused`` / ``failed`` file counts, ``bytes_src`` / ``bytes_out``, ``target_path`` and ``quota``.
    """
    cfg = get_config()
    target = (target if target is not None else cfg["target"]) or ""
    fmt = fmt or cfg["format"]
    level = level or cfg["level"]
    if not target:
        raise DriveArchiveError("no Drive target folder")
    if fmt not in FORMATS or level not in LEVELS:
        raise DriveArchiveError("bad format or level")
    job_dir = build_result["job_dir"]
    store_folder = store.folder
    if ledger is None:
        ledger = CopyLedger(store_folder)
    code = safe_code_token(build_result.get("job_no") or job_id, fallback=job_id, max_len=24)
    photo_by_path = {os.path.normcase(os.path.abspath(p["archive_path"])): p for p in build_result.get("photos", ())
                     if p.get("archive_path")}

    # 1. the files to carry, with the bytes that will really be written
    items = []   # (zip/rel name, bytes, photo dict or None, source size)
    used = set()
    for dirpath, _dirs, files in os.walk(job_dir):
        for fn in sorted(files):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, job_dir).replace("\\", "/")
            with open(full, "rb") as f:
                raw = f.read()
            photo = photo_by_path.get(os.path.normcase(os.path.abspath(full)))
            data = compress_jpeg(raw, level) if photo else raw
            if fmt == "aes":
                ext = os.path.splitext(fn)[1].lower()
                name = fn if photo else (f"{code}{ext}" if ext in (".xlsx", ".pdf") else safe_win_name(fn))
                name = _unique(name, used)
            else:
                name = rel
            items.append((name, data, photo, len(raw)))
    if not items:
        raise DriveArchiveError("empty archive")
    bytes_src = sum(i[3] for i in items)
    fingerprint = hashlib.sha256(
        json.dumps([fmt, level] + [[n, hashlib.sha256(d).hexdigest()] for n, d, _p, _s in items]).encode()
    ).hexdigest()
    prev = load_state(store_folder)["jobs"].get(job_id) or {}

    res = {"job_id": job_id, "format": fmt, "level": level, "written": 0, "reused": 0, "failed": 0,
           "bytes_src": bytes_src, "bytes_out": 0, "ok": False, "archived": False, "quota": quota_status(target)}
    os.makedirs(target, exist_ok=True)
    work = tempfile.mkdtemp(prefix="biruni_drive_")
    verified_photos = []
    try:
        if fmt == "folder":
            root = os.path.join(target, os.path.basename(os.path.normpath(job_dir)))
            res["target_path"] = root
            for name, data, photo, _sz in items:
                dst = os.path.join(root, *name.split("/"))
                want = hashlib.sha256(data).hexdigest()
                res["bytes_out"] += len(data)
                if os.path.isfile(dst) and sha256_file(dst) == want:
                    res["reused"] += 1
                    ok = True
                else:
                    tmp_src = os.path.join(work, "f.bin")
                    with open(tmp_src, "wb") as f:
                        f.write(data)
                    ok = _put_verified(tmp_src, dst, want)
                    res["written" if ok else "failed"] += 1
                if ok and photo:
                    verified_photos.append(photo)
        else:
            if fmt == "aes":
                password = password or load_password()
                if not password:
                    raise DriveArchiveError("no archive password (enable the encrypted ZIP option first)")
                dst = os.path.join(target, f"{code}.encrypted.zip")
            else:
                dst = os.path.join(target, f"{code}.zip")
            res["target_path"] = dst
            if (prev.get("fingerprint") == fingerprint and prev.get("archived") and os.path.isfile(dst)
                    and sha256_file(dst) == prev.get("sha")):
                res["reused"] = len(items)
                res["bytes_out"] = os.path.getsize(dst)
                verified_photos = [i[2] for i in items if i[2]]
            else:
                local = os.path.join(work, "job.zip")
                if fmt == "aes":
                    write_aes_zip(local, [(n, d) for n, d, _p, _s in items], password)
                else:
                    import zipfile

                    with zipfile.ZipFile(local, "w", zipfile.ZIP_DEFLATED) as zf:
                        for n, d, _p, _s in items:
                            zf.writestr(n, d)
                want = sha256_file(local)
                ok = _put_verified(local, dst, want)
                if ok:   # deep check of what is now on the Drive folder
                    try:
                        if fmt == "aes":
                            back = read_aes_zip(dst, password)
                            ok = all(back.get(n) == d for n, d, _p, _s in items)
                        else:
                            import zipfile

                            with zipfile.ZipFile(dst) as zf:
                                ok = zf.testzip() is None
                    except Exception:  # noqa: BLE001
                        ok = False
                    if not ok and os.path.exists(dst):
                        os.remove(dst)
                if ok:
                    res["written"] = len(items)
                    res["bytes_out"] = os.path.getsize(dst)
                    verified_photos = [i[2] for i in items if i[2]]
                else:
                    res["failed"] = len(items)
                res["sha"] = want
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # only a byte-identical copy protects the original: a recompressed photo is a lighter archive, not a second copy
    # (the 2-copy delete rule must never count it)
    for photo in (verified_photos if level == "original" else []):
        if photo.get("id"):
            ledger.mark(photo["id"], PLACE)
    res["ok"] = res["failed"] == 0
    res["archived"] = res["ok"]
    if res["ok"]:
        sha = res.get("sha") or prev.get("sha") or ""
        _save_job_state(store_folder, job_id, {
            "archived": True, "ts": int(time.time() * 1000), "format": fmt, "level": level,
            "target": res["target_path"], "fingerprint": fingerprint, "sha": sha if fmt != "folder" else "",
            "files": len(items),
        })
    res["quota"] = quota_status(target)
    return res


def schedule_after_archive(store, job_id, build_result, ledger=None):
    """Hook for `close_job` and the "Build archive" button: when the office option is on, run the Drive copy in a
    background thread (never blocking, never a modal dialog).  Returns the thread, or None when the option is off."""
    cfg = get_config()
    if not cfg["enabled"] or not cfg["target"] or not isinstance(build_result, dict) or not build_result.get("job_dir"):
        return None

    def run():
        try:
            archive_job_to_drive(store, job_id, build_result, target=cfg["target"], fmt=cfg["format"],
                                 level=cfg["level"], ledger=ledger)
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=run, name="biruni-drive-archive", daemon=True)
    _THREADS[:] = [x for x in _THREADS if x.is_alive()]
    _THREADS.append(t)
    t.start()
    return t


def wait_all(timeout=60):
    """Join the running background copies (tests and shutdown)."""
    for t in list(_THREADS):
        t.join(timeout)


# ---- dock controls -------------------------------------------------------------------------------------------------
from qgis.PyQt.QtCore import QUrl, Qt  # noqa: E402
from qgis.PyQt.QtGui import QDesktopServices  # noqa: E402
from qgis.PyQt.QtWidgets import (  # noqa: E402
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)


class DriveArchiveBox(QWidget):
    """The office option "Also archive to Google Drive": target folder, format, compression level, free-space meter."""

    def __init__(self, parent=None, start_dir=""):
        super().__init__(parent)
        self.start_dir = start_dir
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.enable = QCheckBox(tr("Also archive to Google Drive"))
        v.addWidget(self.enable)
        row = QHBoxLayout()
        self.target = QLineEdit()
        self.target.setReadOnly(True)
        self.pick_btn = QPushButton(tr("Change…"))
        row.addWidget(self.target, 1)
        row.addWidget(self.pick_btn)
        v.addWidget(QLabel(tr("Folder inside the Google Drive synced folder")))
        v.addLayout(row)
        row2 = QHBoxLayout()
        self.fmt = QComboBox()
        for key, label in (("folder", tr("Open folder")), ("zip", tr("ZIP file")),
                           ("aes", tr("Encrypted ZIP file (password)"))):
            self.fmt.addItem(label, key)
        self.level = QComboBox()
        for key, label in (("original", tr("Original photos")), ("high", tr("High quality (smaller)")),
                           ("compact", tr("Compact (smallest)"))):
            self.level.addItem(label, key)
        row2.addWidget(self.fmt, 1)
        row2.addWidget(self.level, 1)
        v.addLayout(row2)
        self.quota_lbl = QLabel()
        self.quota_lbl.setWordWrap(True)
        self.quota_lbl.setObjectName("GpsHint")
        self.upgrade_btn = QPushButton(tr("Upgrade storage"))
        self.upgrade_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(GOOGLE_STORAGE_URL)))
        v.addWidget(self.quota_lbl)
        v.addWidget(self.upgrade_btn)
        self.reload()
        self.enable.toggled.connect(self._changed)
        self.fmt.currentIndexChanged.connect(self._changed)
        self.level.currentIndexChanged.connect(self._changed)
        self.pick_btn.clicked.connect(self._pick)

    def reload(self):
        cfg = get_config()
        for w in (self.enable, self.fmt, self.level):
            w.blockSignals(True)
        self.enable.setChecked(cfg["enabled"])
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(cfg["format"])))
        self.level.setCurrentIndex(max(0, self.level.findData(cfg["level"])))
        for w in (self.enable, self.fmt, self.level):
            w.blockSignals(False)
        self.target.setText(cfg["target"])
        self.refresh_quota()

    def refresh_quota(self):
        t = get_config()["target"]
        q = quota_status(t) if t else None
        self.quota_lbl.setText(quota_message(q) if q else "")
        self.quota_lbl.setVisible(bool(q))
        self.upgrade_btn.setVisible(bool(q and q.get("warn")))
        return q

    def _changed(self, *_a):
        set_config(enabled=self.enable.isChecked(), fmt=self.fmt.currentData(), level=self.level.currentData())
        if self.enable.isChecked() and self.fmt.currentData() == "aes":
            self._password_once()

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, tr("Folder inside the Google Drive synced folder"),
                                             self.target.text() or self.start_dir)
        if d:
            set_config(target=d)
            self.reload()

    def _password_once(self):
        """Show the archive password once (non-blocking), the first time it is generated."""
        pw, created = ensure_password()
        if not created:
            return
        box = QMessageBox(QMessageBox.Icon.Information, tr("Archive password"),
                          tr("Write down this password now. It is shown only once and opens the encrypted ZIP "
                             "files in 7-Zip or WinRAR:") + "\n\n" + pw, QMessageBox.StandardButton.Ok, self)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.open()
        self._pw_box = box
