"""The photo server: the phone opens a page served by this PC (QR code, no app), takes a
photo, and the page sends the photo with its position, accuracy, compass direction, kind
and note. Every photo is written to the photo folder before the phone is told it arrived,
so a photo the phone saw confirmed is on this PC's disk.

The page keeps photos it could not send (no signal, PC asleep) in the phone's browser
storage and sends them when the connection is back; each photo carries an id made on the
phone, so a photo sent twice is kept once.

Over the local network the page is HTTPS with this PC's own certificate (see
gps/browser.py); from anywhere it goes through a temporary tunnel (tunnel.py) that only
passes the bytes through. A random token in the address keeps everyone else out.
"""
from collections import deque
import datetime as dt
import hashlib
import json
import os
import queue
import re
import secrets
import ssl
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from ..i18n import tr
from ..local_server import ExclusiveServer
from .capture import DEV_RE as CAP_DEV_RE, JOB_ID_RE, clean_cap

PHOTO_PORT = 10112
MAX_PHOTO = 40 * 1024 * 1024
LOG_NAME = "photos.jsonl"
BLOCKED_NAME = "blocked_devices.json"
REJECTED_NAME = "rejected.jsonl"
SURVEYOR_KEYS_NAME = "surveyor_keys.json"
ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic", "image/heif": "heif"}


class BlockedDeviceError(PermissionError):
    """Raised when a photo comes from a device_id that the computer has blocked."""

    def __init__(self, device_id, reason=""):
        super().__init__(reason or "device blocked")
        self.device_id = device_id
        self.reason = reason or "device blocked"


def sniff(data):
    """File extension from the first bytes, or None when it is not a photo."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heis", b"hevc"):
        return "heic"
    return None


def _num(v, lo=None, hi=None):
    if v is None or isinstance(v, bool):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v != v or (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return v


def _text(v, n):
    return str(v).strip()[:n] if v not in (None, "") else ""


def clean_meta(m, form=()):
    """Checked record of what the phone sent; ValueError when it cannot be kept."""
    if not isinstance(m, dict) or not ID_RE.match(str(m.get("id", ""))):
        raise ValueError("bad id")
    lat, lon = _num(m.get("lat"), -90, 90), _num(m.get("lon"), -180, 180)
    if lat is None or lon is None:
        raise ValueError("no position")
    t = _num(m.get("t"), 0)
    if t and m.get("tu") == 1:
        dt_corr = _num(m.get("dt"))
        if dt_corr is not None:
            dt_corr = max(-30.0 * 86400 * 1000, min(30.0 * 86400 * 1000, dt_corr))
            t = max(0.0, t + dt_corr)
    when = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc) if t else dt.datetime.now(dt.timezone.utc)
    by = _text(m.get("surveyor") or m.get("by"), 40)
    dev_id = _text(m.get("device_id"), 64)
    if dev_id and not DEV_RE.match(dev_id):
        dev_id = ""
    route = str(m.get("route") or "")
    route = route if route in ("lan", "direct", "relay", "peer", "drive") else ("relay" if m.get("preview") else "")
    out = {"id": m["id"], "lat": lat, "lon": lon, "acc": _num(m.get("acc"), 0),
           "alt": _num(m.get("alt")), "heading": _num(m.get("heading"), 0, 360),
           "compass": _num(m.get("compass"), 0, 360), "fix_age": _num(m.get("fix_age")),
           "taken": when.isoformat(timespec="seconds"), "kind": _text(m.get("kind"), 80),
           "note": _text(m.get("note"), 500), "device": _text(m.get("device"), 80), "by": by,
           "surveyor": by or tr("Unknown"), "device_id": dev_id, "route": route,
           "fields": clean_values(form, m.get("f")),
           # a position averaged on the phone: readings kept and their spread in metres
           "avg_n": _num(m.get("avg_n"), 1, 100000), "avg_sd": _num(m.get("avg_sd"), 0, 10000),
           # inside a building: the level and the room whose code was scanned
           "level": _int(m.get("level"), -20, 300), "unit": _text(m.get("unit"), 40),
           # relay fallback sends a small preview first; the original replaces it when a direct link opens
           "preview": bool(m.get("preview"))}
    cap = clean_cap(m.get("cap"))
    if cap is not None:
        out["cap"] = cap
    return out


def _int(v, lo, hi):
    v = _num(v, lo, hi)
    return int(v) if v is not None and v == int(v) else None


DEV_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def clean_pos(m):
    """A phone's own position for the team map; None when it cannot be used."""
    if not isinstance(m, dict) or not DEV_RE.match(str(m.get("id", ""))):
        return None
    lat, lon = _num(m.get("lat"), -90, 90), _num(m.get("lon"), -180, 180)
    if lat is None or lon is None:
        return None
    t = _num(m.get("t"), 0)
    if t and m.get("tu") == 1:
        dt_corr = _num(m.get("dt"))
        if dt_corr is not None:
            dt_corr = max(-30.0 * 86400 * 1000, min(30.0 * 86400 * 1000, dt_corr))
            t = max(0.0, t + dt_corr)
    when = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc) if t else dt.datetime.now(dt.timezone.utc)
    net, eff, route = str(m.get("net") or ""), str(m.get("eff") or ""), str(m.get("route") or "")
    tm = str(m.get("tm") or "")
    return {"id": m["id"], "name": _text(m.get("name"), 40), "lat": lat, "lon": lon, "acc": _num(m.get("acc"), 0),
            "heading": _num(m.get("heading"), 0, 360), "cog": _num(m.get("cog"), 0, 360),
            "seen": when.isoformat(timespec="seconds"),
            "alt": _num(m.get("alt")), "speed": _num(m.get("speed"), 0, 200), "t": t,
            "sk": _text(m.get("sk"), 64),
            "tm": tm if tm in ("off", "photo", "1", "5", "15", "live") else "",
            # the phone's link, for the team list: Wi-Fi or mobile data, the time to the computer and back
            # (ms; measured by the phone, or by this computer over the bridge), the route, the battery
            "net": net if net in NETS else "", "eff": eff if eff in ("slow-2g", "2g", "3g", "4g") else "",
            "down": _num(m.get("down"), 0, 10000), "ping": _num(m.get("ping"), 0, 120000),
            "crtt": _num(m.get("crtt"), 0, 60000),
            "route": route if route in ("lan", "direct", "relay", "peer", "mqtt", "org", "own", "tunnel", "drive") else "",
            "bat": _num(m.get("bat"), 0, 1), "chg": m.get("chg") is True}


HS_MODES = ("off", "photo", "1", "5", "15", "live")


def clean_hs(m):
    """A coordinate-free status heartbeat from the phone (`hs`); None when invalid."""
    if not isinstance(m, dict):
        return None
    d = str(m.get("d") or m.get("id") or "")
    if not DEV_RE.match(d):
        return None
    raw_q = m.get("q")
    if isinstance(raw_q, (list, tuple)) and len(raw_q) == 4:
        q = []
        for x in raw_q:
            v = _int(x, 0, 1_000_000)
            q.append(v if v is not None else 0)
    else:
        q = [0, 0, 0, 0]
    old = _num(m.get("old"), 0)
    old = int(old) if old else None
    lp = _int(m.get("lp"), 0, 1_000_000)
    tm = str(m.get("tm") or "")
    tm = tm if tm in HS_MODES else ""
    cs = _int(m.get("cs"), 0, 1_000_000_000)
    ps = m.get("ps") if isinstance(m.get("ps"), bool) else None
    t = _num(m.get("t"), 0)
    t = int(t) if t else int(time.time() * 1000)
    route = str(m.get("route") or "")
    route = route if route in ("lan", "direct", "relay", "peer", "mqtt", "org", "own", "tunnel", "drive") else ""
    return {
        "id": d,
        "d": d,
        "name": _text(m.get("n") or m.get("name"), 40),
        "n": _text(m.get("n") or m.get("name"), 40),
        "sk": _text(m.get("sk"), 64),
        "q": q,
        "old": old,
        "lp": lp if lp is not None else 0,
        "tm": tm,
        "cs": cs if cs is not None else 0,
        "ps": ps,
        "t": t,
        "route": route,
    }


NETS = ("wifi", "cellular", "ethernet", "bluetooth", "wimax", "none", "other", "unknown")


MAX_GUIDE = 200_000  # bytes: goes to the phones in one message


def guide_bytes(v, items):
    """The guide the PC sends to the phones: points to reach, lines and boundaries to walk.
    items: [{"k": "pt"|"line"|"poly", "n": name, "c": coordinates (lon, lat)}]."""
    def r(c):
        return [round(c[0], 7), round(c[1], 7)]

    out, size = [], 0
    for it in items:
        k = it.get("k")
        if k == "pt":
            c = r(it["c"])
        elif k in ("line", "poly"):
            c = [[r(x) for x in part] for part in it["c"] if len(part) >= (3 if k == "poly" else 2)]
            if not c:
                continue
        else:
            continue
        one = {"k": k, "n": _text(it.get("n"), 60), "c": c}
        size += len(json.dumps(one, ensure_ascii=False).encode("utf-8"))
        if size > MAX_GUIDE:
            break
        out.append(one)
    return json.dumps({"v": v, "items": out}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), len(out)


MAX_INDOOR = 400_000  # bytes: the rooms of a building, sent once


def indoor_payload(levels, units):
    """The indoor map for the phones: where each room is, so a scanned room code places the photo.
    levels: [{"id": int, "name": str}]; units: [{"id", "lv", "n", "num", "cat", "c": rings (lon, lat),
    "p": [lon, lat] a point inside, "r": metres from that point to the far side}]."""
    def r(c):
        return [round(c[0], 7), round(c[1], 7)]

    out, size = [], 0
    for u in units:
        uid = _text(u.get("id"), 40)
        rings = [[r(x) for x in ring] for ring in u.get("c") or () if len(ring) >= 3]
        if not uid or not rings or not u.get("p"):
            continue
        one = {"id": uid, "lv": int(u.get("lv") or 0), "n": _text(u.get("n"), 60), "num": _text(u.get("num"), 20),
               "cat": _text(u.get("cat"), 20), "c": rings, "p": r(u["p"]), "r": round(float(u.get("r") or 5), 1)}
        size += len(json.dumps(one, ensure_ascii=False).encode("utf-8"))
        if size > MAX_INDOOR:
            break
        out.append(one)
    lv = [{"id": int(x["id"]), "name": _text(x.get("name"), 40)} for x in levels]
    return {"levels": lv, "units": out}


# ---- the form: questions the user designs on the PC, answered on the phone with each photo ----
FIELD_TYPES = ("text", "number", "integer", "choice", "multi", "yesno", "date")
MAX_FIELDS, MAX_CHOICES = 30, 50
# columns the photos layer has already (dock.FIELDS) - a question never takes one of these names
RESERVED = {"fid", "geom", "geometry", "photo_id", "photo", "taken", "kind", "note", "accuracy_m", "altitude",
            "compass", "heading", "fix_age_s", "device", "avg_n", "avg_sd_m", "level_id", "unit_id",
            "surveyor", "device_id", "job_id", "job_item"}
NAME_RE = re.compile(r"^[^\W\d]\w{0,29}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def column_name(label, taken):
    """A column name for a question: its words joined by _, not used yet."""
    label = "".join(c for c in label if unicodedata.category(c) != "Mn")  # tashkeel and accents are not letters
    n = re.sub(r"\W+", "_", label.strip().lower()).strip("_")[:30] or "field"
    if not NAME_RE.match(n):
        n = ("f_" + n)[:30]
    base, i = n, 2
    while n in taken or n in RESERVED:
        n = f"{base[:26]}_{i}"
        i += 1
    return n


def clean_form(form):
    """The form as Biruni keeps it: at most 30 questions, each with a label, a type and a
    column name that never changes (renaming a question keeps its column)."""
    out, taken = [], set()
    for f in form if isinstance(form, list) else ():
        if not isinstance(f, dict) or not _text(f.get("label"), 60):
            continue
        t = f.get("type") if f.get("type") in FIELD_TYPES else "text"
        name = _text(f.get("name"), 30)
        if not NAME_RE.match(name) or name in taken or name in RESERVED:
            name = column_name(_text(f.get("label"), 60), taken)
        taken.add(name)
        d = {"name": name, "label": _text(f.get("label"), 60), "type": t,
             "req": bool(f.get("req")), "keep": bool(f.get("keep"))}
        if t in ("choice", "multi"):
            choices = []
            for c in f.get("choices") or ():
                c = _text(c, 60)
                if c and c not in choices:
                    choices.append(c)
            if not choices:
                continue
            d["choices"] = choices[:MAX_CHOICES]
        elif t in ("number", "integer"):
            lo, hi = _num(f.get("min")), _num(f.get("max"))
            if lo is not None and hi is not None and lo > hi:
                lo, hi = hi, lo
            d["min"], d["max"] = lo, hi
        out.append(d)
        if len(out) == MAX_FIELDS:
            break
    return out


def clean_values(form, values):
    """The answers that fit the form, checked again here (the phone checks them first). A value
    that does not fit is left out - the photo itself is always kept."""
    out = {}
    values = values if isinstance(values, dict) else {}
    for f in form:
        v, t = values.get(f["name"]), f["type"]
        if v is None or v == "" or v == []:
            continue
        if t == "text":
            v = _text(v, 250)
        elif t in ("number", "integer"):
            v = _num(v, f.get("min"), f.get("max"))
            if v is None or (t == "integer" and v != int(v)):
                continue
            v = int(v) if t == "integer" else v
        elif t == "choice":
            v = _text(v, 60)  # a choice removed since the photo was taken is still kept
        elif t == "multi":
            v = "; ".join(x for x in (_text(x, 60) for x in (v if isinstance(v, list) else [v])) if x)[:500]
        elif t == "yesno":
            if not isinstance(v, bool):
                continue
        elif t == "date":
            v = _text(v, 10)
            try:
                dt.date.fromisoformat(v)
            except ValueError:
                continue
            if not DATE_RE.match(v):
                continue
        if v != "":
            out[f["name"]] = v
    return out


def read_blocked(folder):
    """Blocked device_ids of the folder: {device_id: {"reason": str, "at": str, "name": str}}."""
    try:
        with open(os.path.join(folder, BLOCKED_NAME), encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_blocked(folder, blocked):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, BLOCKED_NAME)
    part = path + ".part"
    with open(part, "w", encoding="utf-8") as f:
        json.dump(blocked, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, path)


def read_rejected(folder):
    """Every rejected photo attempt logged in rejected.jsonl."""
    out = []
    try:
        with open(os.path.join(folder, REJECTED_NAME), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("device_id"):
                    out.append(r)
    except OSError:
        pass
    return out


def read_surveyor_keys(folder):
    """Surveyor QR mode and per-surveyor secret keys:
    {"qr_mode": "shared" | "per_surveyor", "surveyors": {srv_id: {"id", "name", "secret", "revoked", "reason", "at"}}}."""
    try:
        with open(os.path.join(folder, SURVEYOR_KEYS_NAME), encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict):
                mode = d.get("qr_mode") if d.get("qr_mode") in ("shared", "per_surveyor") else "shared"
                survs = d.get("surveyors") if isinstance(d.get("surveyors"), dict) else {}
                return {"qr_mode": mode, "surveyors": survs}
    except (OSError, ValueError):
        pass
    return {"qr_mode": "shared", "surveyors": {}}


def write_surveyor_keys(folder, data):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, SURVEYOR_KEYS_NAME)
    part = path + ".part"
    with open(part, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, path)


class PhotoStore:
    """The photo folder: one sub-folder per day, plus photos.jsonl (one line per photo)
    that lets the map layer be rebuilt if Biruni stops between the two writes."""

    def __init__(self, folder, form=(), on_job=None, mirror_folder=""):
        self.folder = folder
        self.form = clean_form(list(form))
        self.on_job = on_job
        self.lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)
        self.records = {r["id"]: r for r in read_log(folder)}
        self.blocked = read_blocked(folder)
        self.surveyor_keys = read_surveyor_keys(folder)
        self._jobs = None
        from .copies import CopyLedger

        self.ledger = CopyLedger(folder, mirror_folder=mirror_folder)
        for r in self.records.values():
            cur = self.ledger.get(r["id"])
            if cur:
                if not r.get("sha") and cur.get("sha"):
                    r["sha"] = cur["sha"]
                if not r.get("ssha") and cur.get("ssha"):
                    r["ssha"] = cur["ssha"]
            if not r.get("preview") and r.get("photo") and os.path.isfile(r["photo"]):
                places = ["office"]
                if r.get("route") == "drive":
                    places.append("drive_mb")
                self.ledger.mark(r["id"], places=places, sha=r.get("sha") or "", ssha=r.get("ssha") or "")

    def set_mirror_folder(self, mirror_folder):
        self.ledger.set_mirror_folder(mirror_folder)
        if self.ledger.mirror_folder:
            self.ledger.sync_all_to_mirror(self.records.values())

    @property
    def jobs(self):
        if self._jobs is None:
            from .capture_jobs import JobStore

            self._jobs = JobStore(self.folder)
        return self._jobs

    def originals(self, ids, for_drive_clean=False):
        """Of these photo ids, those kept here as full originals (not only a preview), plus merged state item ids:
        the phone may then free its copy in the surveyor's Google Drive when 2 non-mailbox copies exist
        (drive_page.py, receipts over the line)."""
        state_ids = self.jobs.state_ids()
        out = []
        for i in ids:
            if not isinstance(i, str):
                continue
            r = self.records.get(i)
            has_orig = (r and not r.get("preview") and r.get("photo") and os.path.isfile(r["photo"])) or i in state_ids
            if not has_orig:
                continue
            if for_drive_clean and self.ledger.non_mailbox_count(i) < 2:
                continue
            out.append(i)
        return out

    def copy_status(self, ids):
        """Return [[id, count, h8], ...] for the given item ids from the CopyLedger."""
        state_ids = None
        out = []
        for i in ids:
            if not isinstance(i, str) or not i or len(i) >= 80:
                continue
            c, _h, h8 = self.ledger.receipt_info(i)
            if c == 0:
                r = self.records.get(i)
                if r and not r.get("preview") and r.get("photo") and os.path.isfile(r["photo"]):
                    places = ["office"]
                    if r.get("route") == "drive":
                        places.append("drive_mb")
                    self.ledger.mark(i, places=places, sha=r.get("sha") or "", ssha=r.get("ssha") or "")
                    c, _h, h8 = self.ledger.receipt_info(i)
                else:
                    if state_ids is None:
                        state_ids = self.jobs.state_ids()
                    if i in state_ids:
                        self.ledger.mark(i, "office")
                        c, _h, h8 = self.ledger.receipt_info(i)
            out.append([i, c, h8])
        return out

    def get_qr_mode(self):
        with self.lock:
            self.surveyor_keys = read_surveyor_keys(self.folder)
            return self.surveyor_keys.get("qr_mode", "shared")

    def set_qr_mode(self, mode):
        mode = "per_surveyor" if mode == "per_surveyor" else "shared"
        with self.lock:
            self.surveyor_keys = read_surveyor_keys(self.folder)
            self.surveyor_keys["qr_mode"] = mode
            write_surveyor_keys(self.folder, self.surveyor_keys)
        return mode

    def issue_surveyor_key(self, name, surveyor_id=None, rotate=False):
        """Create or return a personal QR secret key for a surveyor; rotate=True issues a fresh secret and invalidates the old one."""
        name = _text(name, 40)
        if not name:
            raise ValueError("surveyor name required")
        with self.lock:
            self.surveyor_keys = read_surveyor_keys(self.folder)
            survs = self.surveyor_keys.setdefault("surveyors", {})
            existing = None
            if surveyor_id and surveyor_id in survs:
                existing = survs[surveyor_id]
            else:
                for s in survs.values():
                    if s.get("name", "").lower() == name.lower():
                        existing = s
                        break
            if existing is not None and not rotate and not existing.get("revoked"):
                existing["name"] = name
                write_surveyor_keys(self.folder, self.surveyor_keys)
                return dict(existing)
            sid = existing["id"] if existing is not None else ("srv_" + secrets.token_hex(6))
            entry = {
                "id": sid,
                "name": name,
                "secret": secrets.token_urlsafe(16),
                "revoked": False,
                "reason": "",
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            }
            survs[sid] = entry
            write_surveyor_keys(self.folder, self.surveyor_keys)
            self.blocked = read_blocked(self.folder)
            if self.blocked.pop(sid, None) is not None:
                write_blocked(self.folder, self.blocked)
            return dict(entry)

    def list_surveyor_keys(self):
        with self.lock:
            self.surveyor_keys = read_surveyor_keys(self.folder)
            return [dict(v) for v in self.surveyor_keys.get("surveyors", {}).values()]

    def revoke_surveyor_key(self, target, reason=""):
        """Revoke a surveyor's personal QR key (by id, name or secret) without affecting any other surveyor."""
        t = _text(target, 64)
        if not t:
            return None
        reason_txt = _text(reason, 200) or tr("Surveyor QR code revoked")
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with self.lock:
            self.surveyor_keys = read_surveyor_keys(self.folder)
            survs = self.surveyor_keys.get("surveyors", {})
            hit = None
            for s in survs.values():
                if s.get("id") == t or s.get("secret") == t or s.get("name", "").lower() == t.lower():
                    hit = s
                    break
            if hit is None:
                return None
            hit["revoked"] = True
            hit["reason"] = reason_txt
            hit["revoked_at"] = now_iso
            write_surveyor_keys(self.folder, self.surveyor_keys)
            self.blocked = read_blocked(self.folder)
            self.blocked[hit["id"]] = {"reason": reason_txt, "name": hit.get("name", ""), "at": now_iso}
            write_blocked(self.folder, self.blocked)
            return dict(hit)

    def unrevoke_surveyor_key(self, target):
        t = _text(target, 64)
        if not t:
            return None
        with self.lock:
            self.surveyor_keys = read_surveyor_keys(self.folder)
            survs = self.surveyor_keys.get("surveyors", {})
            hit = None
            for s in survs.values():
                if s.get("id") == t or s.get("secret") == t or s.get("name", "").lower() == t.lower():
                    hit = s
                    break
            if hit is None:
                return None
            hit["revoked"] = False
            hit["reason"] = ""
            write_surveyor_keys(self.folder, self.surveyor_keys)
            self.blocked = read_blocked(self.folder)
            if self.blocked.pop(hit["id"], None) is not None:
                write_blocked(self.folder, self.blocked)
            return dict(hit)

    def resolve_pos(self, pos):
        """Authenticate/bind a phone position update against surveyor_keys and blocked_devices."""
        if not isinstance(pos, dict):
            return None
        sk = _text(pos.get("sk"), 64)
        pos = dict(pos)
        pos.pop("sk", None)
        with self.lock:
            self.surveyor_keys = read_surveyor_keys(self.folder)
            self.blocked = read_blocked(self.folder)
            mode = self.surveyor_keys.get("qr_mode", "shared")
            survs = self.surveyor_keys.get("surveyors", {})
            matched = None
            if sk:
                for s in survs.values():
                    sec = str(s.get("secret") or "")
                    if sec and secrets.compare_digest(sec, sk):
                        matched = s
                        break
                if matched is None:
                    return None
                if matched.get("revoked") or matched.get("id") in self.blocked:
                    return None
                pos["id"] = matched["id"]
                pos["name"] = matched["name"]
                return pos
            if mode == "per_surveyor":
                return None
            if pos.get("id") in self.blocked:
                return None
            return pos

    def block_device(self, device_id, reason="", name=""):
        """Block a device_id (and revoke any matching personal surveyor key) so future photos from it are refused and logged; existing photos are kept."""
        device_id = _text(device_id, 64)
        if not device_id:
            return
        reason_txt = _text(reason, 200) or tr("Blocked by the computer")
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with self.lock:
            self.blocked = read_blocked(self.folder)
            self.blocked[device_id] = {
                "reason": reason_txt,
                "name": _text(name, 40),
                "at": now_iso,
            }
            write_blocked(self.folder, self.blocked)
            self.surveyor_keys = read_surveyor_keys(self.folder)
            survs = self.surveyor_keys.get("surveyors", {})
            changed = False
            for s in survs.values():
                if s.get("id") == device_id or (name and s.get("name", "").lower() == _text(name, 40).lower()):
                    s["revoked"] = True
                    s["reason"] = reason_txt
                    s["revoked_at"] = now_iso
                    changed = True
            if changed:
                write_surveyor_keys(self.folder, self.surveyor_keys)

    def unblock_device(self, device_id):
        device_id = _text(device_id, 64)
        with self.lock:
            self.blocked = read_blocked(self.folder)
            self.blocked.pop(device_id, None)
            write_blocked(self.folder, self.blocked)
            self.surveyor_keys = read_surveyor_keys(self.folder)
            survs = self.surveyor_keys.get("surveyors", {})
            if device_id in survs:
                survs[device_id]["revoked"] = False
                survs[device_id]["reason"] = ""
                write_surveyor_keys(self.folder, self.surveyor_keys)

    def is_blocked(self, device_id):
        with self.lock:
            self.blocked = read_blocked(self.folder)
            return bool(device_id and device_id in self.blocked)

    def unify_name(self, new_name, device_id=None, old_names=()):
        """Unify surveyor name across matching records in memory, photos.jsonl, and surveyor_keys.json."""
        new_name = _text(new_name, 40)
        if not new_name:
            return 0
        old_set = {_text(x, 40) for x in (old_names or ()) if _text(x, 40)}
        changed = 0
        with self.lock:
            for r in self.records.values():
                if (device_id and r.get("device_id") == device_id) or (
                    old_set and (r.get("surveyor") in old_set or r.get("by") in old_set)
                ):
                    r["surveyor"] = new_name
                    r["by"] = new_name
                    changed += 1
            if changed:
                path = os.path.join(self.folder, LOG_NAME)
                part = path + ".part"
                with open(part, "w", encoding="utf-8") as f:
                    for r in self.records.values():
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(part, path)
            self.surveyor_keys = read_surveyor_keys(self.folder)
            survs = self.surveyor_keys.get("surveyors", {})
            sk_changed = False
            for s in survs.values():
                if (device_id and s.get("id") == device_id) or (old_set and s.get("name") in old_set):
                    s["name"] = new_name
                    sk_changed = True
            if sk_changed:
                write_surveyor_keys(self.folder, self.surveyor_keys)
        return changed

    def _check_sender(self, rec, sk=""):
        """Verify device/sk/blocked rules for a photo or state item; binds verified surveyor into rec or raises BlockedDeviceError."""
        self.blocked = read_blocked(self.folder)
        self.surveyor_keys = read_surveyor_keys(self.folder)
        mode = self.surveyor_keys.get("qr_mode", "shared")
        survs = self.surveyor_keys.get("surveyors", {})
        matched_srv = None
        if sk:
            for s in survs.values():
                sec = str(s.get("secret") or "")
                if sec and secrets.compare_digest(sec, sk):
                    matched_srv = s
                    break
            if matched_srv is None:
                reason = tr("Invalid or replaced surveyor QR code")
                dev_id = rec.get("device_id") or "invalid_qr"
                entry = {
                    "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "id": rec["id"],
                    "device_id": dev_id,
                    "surveyor": rec.get("surveyor") or "",
                    "reason": reason,
                }
                with open(os.path.join(self.folder, REJECTED_NAME), "a", encoding="utf-8") as rf:
                    rf.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    rf.flush()
                raise BlockedDeviceError(dev_id, reason)
            # Bind surveyor name and device_id to the PC's verified surveyor record (cannot be spoofed on the phone)
            rec["surveyor"] = matched_srv["name"]
            rec["by"] = matched_srv["name"]
            rec["device_id"] = matched_srv["id"]
            if matched_srv.get("revoked"):
                reason = matched_srv.get("reason") or tr("Surveyor QR code revoked")
                entry = {
                    "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "id": rec["id"],
                    "device_id": matched_srv["id"],
                    "surveyor": matched_srv["name"],
                    "reason": reason,
                }
                with open(os.path.join(self.folder, REJECTED_NAME), "a", encoding="utf-8") as rf:
                    rf.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    rf.flush()
                raise BlockedDeviceError(matched_srv["id"], reason)
        elif mode == "per_surveyor":
            reason = tr("Each surveyor must use their own personal QR code")
            dev_id = rec.get("device_id") or "unregistered_qr"
            entry = {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "id": rec["id"],
                "device_id": dev_id,
                "surveyor": rec.get("surveyor") or "",
                "reason": reason,
            }
            with open(os.path.join(self.folder, REJECTED_NAME), "a", encoding="utf-8") as rf:
                rf.write(json.dumps(entry, ensure_ascii=False) + "\n")
                rf.flush()
            raise BlockedDeviceError(dev_id, reason)
        dev_id = rec.get("device_id") or ""
        if dev_id and dev_id in self.blocked:
            info = self.blocked[dev_id]
            reason = (info.get("reason") if isinstance(info, dict) else str(info)) or tr("Blocked by the computer")
            entry = {
                "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "id": rec["id"],
                "device_id": dev_id,
                "surveyor": rec.get("surveyor") or "",
                "reason": reason,
            }
            with open(os.path.join(self.folder, REJECTED_NAME), "a", encoding="utf-8") as rf:
                rf.write(json.dumps(entry, ensure_ascii=False) + "\n")
                rf.flush()
            raise BlockedDeviceError(dev_id, reason)

    def _save_state_item(self, meta, data, ssha=""):
        if not isinstance(meta, dict) or not ID_RE.match(str(meta.get("id", ""))):
            raise ValueError("bad id")
        raw_blob = bytes(data) if isinstance(data, (bytes, bytearray)) else b""
        sha = hashlib.sha256(raw_blob).hexdigest()
        ssha = str(ssha or meta.get("_ssha") or "")
        raw_state = None
        if raw_blob[:4] == b"BCS1":
            try:
                raw_state = json.loads(raw_blob[4:].decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raw_state = None
        by = _text(meta.get("surveyor") or meta.get("by") or (raw_state.get("by") if isinstance(raw_state, dict) else ""), 40)
        raw_dev_meta = meta.get("device_id")
        raw_dev_state = raw_state.get("dev") if isinstance(raw_state, dict) else None
        dev_cand = str(raw_dev_meta if raw_dev_meta not in (None, "") else (raw_dev_state or "")).strip()
        rec = {
            "id": meta["id"],
            "by": by,
            "surveyor": by or tr("Unknown"),
            "device_id": dev_cand[:64],
        }
        sk = _text(meta.get("sk"), 64)
        with self.lock:
            self._check_sender(rec, sk)
        cs = meta.get("cs")
        if not isinstance(cs, dict) or raw_blob[:4] != b"BCS1" or not isinstance(raw_state, dict):
            raise ValueError("bad state item")
        raw_j = cs.get("j")
        if not isinstance(raw_j, str) or not JOB_ID_RE.fullmatch(raw_j.strip()):
            raise ValueError("bad job id")
        job_id = raw_j.strip()
        if raw_state.get("job") not in (None, "") and str(raw_state.get("job")).strip() != job_id:
            raise ValueError("job id mismatch")
        if "rev" in cs and "rev" in raw_state and cs["rev"] != raw_state["rev"]:
            raise ValueError("rev mismatch")
        if sk:
            dev = rec["device_id"]
        else:
            if raw_dev_meta not in (None, "") and not (isinstance(raw_dev_meta, str) and CAP_DEV_RE.fullmatch(raw_dev_meta.strip())):
                raise ValueError("bad dev id")
            if raw_dev_state not in (None, "") and not (isinstance(raw_dev_state, str) and CAP_DEV_RE.fullmatch(raw_dev_state.strip())):
                raise ValueError("bad dev id")
            if (raw_dev_meta not in (None, "") and raw_dev_state not in (None, "")
                    and raw_dev_meta.strip() != raw_dev_state.strip()):
                raise ValueError("dev id mismatch")
            dev = dev_cand
        if not isinstance(dev, str) or not CAP_DEV_RE.fullmatch(dev):
            raise ValueError("bad dev id")
        state_in = dict(raw_state, job=job_id, dev=dev)
        if rec.get("by"):
            state_in["by"] = rec["by"]
        self.jobs.take_state(job_id, dev, state_in, item_id=rec["id"])
        places = ["office"]
        if meta.get("route") == "drive":
            places.append("drive_mb")
        self.ledger.mark(rec["id"], places=places, sha=sha, ssha=ssha)
        if self.ledger.mirror_folder:
            try:
                _jid, _did, _jdir, spath = self.jobs._safe_state_path(job_id, dev)
                if os.path.isfile(spath):
                    self.ledger.write_mirror_file(rec["id"], spath, ssha=ssha)
            except (ValueError, OSError):
                pass
        if self.on_job is not None:
            self.on_job(job_id)
        return {"id": rec["id"], "sha": sha, "ssha": ssha}, False

    def save(self, meta, data, ssha=""):
        """(record, new) - the photo is on disk when this returns.
        When a preview arrived earlier over the relays and the full original arrives later with the same id,
        the original replaces the preview file in the same record."""
        if isinstance(meta, dict) and ("cs" in meta or (isinstance(data, (bytes, bytearray)) and bytes(data)[:4] == b"BCS1")):
            return self._save_state_item(meta, data, ssha=ssha)
        ext = sniff(data)
        if ext is None:
            raise ValueError("not a photo")
        sha = hashlib.sha256(bytes(data)).hexdigest()
        ssha = str(ssha or (meta.get("_ssha") if isinstance(meta, dict) else "") or "")
        rec = clean_meta(meta, self.form)
        sk = _text(meta.get("sk"), 64) if isinstance(meta, dict) else ""
        with self.lock:
            self._check_sender(rec, sk)
            old = self.records.get(rec["id"])
            if old is not None:
                if not old.get("preview") or rec.get("preview"):
                    if not old.get("preview"):
                        if not old.get("sha"):
                            old["sha"] = sha
                        if ssha and not old.get("ssha"):
                            old["ssha"] = ssha
                        places = ["office"]
                        if rec.get("route") == "drive" or old.get("route") == "drive":
                            places.append("drive_mb")
                        self.ledger.mark(old["id"], places=places, sha=old.get("sha") or sha, ssha=ssha or old.get("ssha") or "")
                        if self.ledger.mirror_folder and old.get("photo") and os.path.isfile(old["photo"]):
                            cur = self.ledger.get(old["id"])
                            if not (cur and cur["places"].get("mirror")):
                                self.ledger.write_mirror_file(
                                    old["id"], old["photo"], expected_sha=old.get("sha") or sha,
                                    ssha=ssha or old.get("ssha") or "",
                                )
                    return old, False
                if not rec.get("by") and old.get("by"):
                    rec["by"] = old["by"]
                    rec["surveyor"] = old.get("surveyor") or old["by"]
                if not rec.get("device_id") and old.get("device_id"):
                    rec["device_id"] = old["device_id"]
                if not rec.get("cap") and old.get("cap"):
                    rec["cap"] = old["cap"]
                # replacing an earlier preview with the full original in the same record
                old_path = old.get("photo") or ""
                base = os.path.splitext(old_path)[0] if old_path else ""
                path = f"{base}.{ext}" if base else ""
                if not path:
                    local = dt.datetime.fromisoformat(rec["taken"]).astimezone()
                    day = os.path.join(self.folder, local.strftime("%Y-%m-%d"))
                    os.makedirs(day, exist_ok=True)
                    path = os.path.join(day, f"{local:%Y%m%d_%H%M%S}_{rec['id'][:8]}.{ext}")
                part = path + ".part"
                with open(part, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(part, path)
                if old_path and old_path != path and os.path.isfile(old_path):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
                rec["photo"] = path
                with open(os.path.join(self.folder, LOG_NAME), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                rec["sha"] = sha
                rec["ssha"] = ssha
                self.records[rec["id"]] = rec
                places = ["office"]
                if rec.get("route") == "drive":
                    places.append("drive_mb")
                self.ledger.mark(rec["id"], places=places, sha=sha, ssha=ssha)
                if self.ledger.mirror_folder:
                    self.ledger.write_mirror_file(rec["id"], path, expected_sha=sha, ssha=ssha)
                return rec, "upgraded"
            local = dt.datetime.fromisoformat(rec["taken"]).astimezone()
            day = os.path.join(self.folder, local.strftime("%Y-%m-%d"))
            os.makedirs(day, exist_ok=True)
            name = f"{local:%Y%m%d_%H%M%S}_{rec['id'][:8]}"
            path = os.path.join(day, f"{name}.{ext}")
            n = 1
            while os.path.exists(path):   # two photos of one second whose ids begin alike: one replaced the other
                n += 1
                path = os.path.join(day, f"{name}_{n}.{ext}")
            part = path + ".part"
            with open(part, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(part, path)
            rec["photo"] = path
            with open(os.path.join(self.folder, LOG_NAME), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            rec["sha"] = sha
            rec["ssha"] = ssha
            self.records[rec["id"]] = rec
            if not rec.get("preview"):
                places = ["office"]
                if rec.get("route") == "drive":
                    places.append("drive_mb")
                self.ledger.mark(rec["id"], places=places, sha=sha, ssha=ssha)
                if self.ledger.mirror_folder:
                    self.ledger.write_mirror_file(rec["id"], path, expected_sha=sha, ssha=ssha)
            return rec, True


def read_log(folder):
    """Every photo record of the folder (a damaged last line - power cut - is skipped)."""
    by_id = {}
    try:
        with open(os.path.join(folder, LOG_NAME), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("id") and r.get("photo"):
                    if not r.get("surveyor"):
                        r["surveyor"] = r.get("by") or tr("Unknown")
                    if "device_id" not in r:
                        r["device_id"] = ""
                    by_id[r["id"]] = r
    except OSError:
        pass
    return list(by_id.values())


class PhotoServer:
    """HTTPS server in a background thread. Events for the GUI go to self.events:
    ("photo", record) for a new photo, ("phone", user agent) when a phone opens the page."""

    def __init__(self, folder, cert_folder, page_texts, lang="en", kinds=(), port=PHOTO_PORT, addresses=(),
                 bind="0.0.0.0", form=(), mirror_folder="", eod_hour=18):
        from ..gps.browser import ensure_certificate

        self.token = secrets.token_urlsafe(12)
        self.port = port
        self.events = queue.Queue()
        self.store = PhotoStore(folder, form, on_job=lambda jid: self.events.put(("job", jid)),
                                mirror_folder=mirror_folder)
        self.cert, self.key = ensure_certificate(cert_folder, list(addresses))
        from .handover import HandoverKey

        self.handover = HandoverKey(cert_folder)   # photos handed phone to phone are sealed for this key
        self.cert_folder = cert_folder
        self.rl = None   # the signed recipients list (recipients.py): set with the bridge key in set_ladder
        self.eod_hour = int(eod_hour) if isinstance(eod_hour, int) and 0 <= eod_hour <= 23 else 18
        self.page = render_page(page_texts, lang, kinds, self.store.form, self.handover.public)
        # the bridge (bridge.py) runs a hidden receiver page on this PC that hands the photos in here
        self.bridge_config = json.dumps({"lang": lang,
                                         "kinds": [k for k in (s.strip() for s in kinds) if k][:24],
                                         "form": self.store.form, "pk": self.handover.public,
                                         "eod": self.eod_hour},
                                        ensure_ascii=False).encode("utf-8")
        self._phones = set()
        self.guide_v = 0
        self.drive_clean = "keep"   # the office's choice for the surveyors' Drive copies: keep / manual / auto
        self.guide = guide_bytes(0, [])[0]
        self.ladder = None
        self.xin = None
        self.xout = None
        self.tunnel_host = ""
        self.org_host = ""
        self._xout_seq = 0
        self._xlock = threading.Lock()
        self._x_fails = deque()
        self._x_block_until = 0.0
        self._stopping = False
        server = self

        def _x_fail():
            now = time.monotonic()
            with server._xlock:
                while server._x_fails and now - server._x_fails[0] > 60.0:
                    server._x_fails.popleft()
                server._x_fails.append(now)
                if len(server._x_fails) >= 30:
                    server._x_block_until = now + 60.0

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 120   # a photo whose sending stalled half way (mobile data, the tunnel) frees its thread

            def log_message(self, *_a):
                pass

            def _route(self):
                parts = urlsplit(self.path)
                bits = parts.path.strip("/").split("/")
                if not bits or not secrets.compare_digest(bits[0], server.token):
                    return None, parts
                return "/".join(bits[1:]), parts

            def _send(self, code, body=b"", ctype="application/json", cors=False):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if cors:
                    self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_HEAD(self):  # noqa: N802
                route, _parts = self._route()
                if route in ("", "ping", "sw.js"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript" if route == "sw.js" else "text/html")
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                else:
                    self._send(404)

            def do_OPTIONS(self):  # noqa: N802
                bits = urlsplit(self.path).path.strip("/").split("/")
                if len(bits) == 2 and bits[0] == "x":
                    self.send_response(204)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                    self.send_header("Access-Control-Allow-Headers", "Content-Type")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._send(404)

            def do_GET(self):  # noqa: N802
                route, parts = self._route()
                if route is None:
                    self._send(404)
                elif route == "" and not parts.path.endswith("/"):
                    self.send_response(301)
                    self.send_header("Location", f"/{server.token}/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif route == "":
                    agent = (self.headers.get("User-Agent") or "")[:160]
                    if agent not in server._phones:
                        server._phones.add(agent)
                        server.events.put(("phone", agent))
                    self._send(200, server.page, "text/html; charset=utf-8")
                elif route == "ping":
                    self._send(200, json.dumps({"ok": True, "nl": 1, "hs": 1, "now": int(time.time() * 1000),
                                                "eod": server.eod_hour}).encode("utf-8"))
                elif route == "manifest.webmanifest":
                    self._send(200, b'{"name":"Biruni GIS","short_name":"Biruni GIS","display":"standalone"}',
                               "application/manifest+json; charset=utf-8")
                elif route in ("icon.png", "icon-512.png"):
                    icon_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resources", "branding",
                                            "logo-1024.png" if route == "icon-512.png" else "logo-256.png")
                    try:
                        with open(icon_src, "rb") as f:
                            self._send(200, f.read(), "image/png")
                    except OSError:
                        self._send(404)
                elif route == "sw.js":
                    self._send(200, b"/* sw */", "application/javascript")
                elif route == "bridge" and self._local():
                    from .bridge import receiver_page

                    self._send(200, receiver_page(), "text/html; charset=utf-8")
                elif route == "guide":
                    self._send(200, server.guide)
                elif route == "bridge-config" and self._local():
                    self._send(200, server._bridge_config_bytes())
                elif route == "x-pull" and self._local():
                    if server.xin is None:
                        self._send(404)
                        return
                    items = []
                    if not server._stopping:
                        try:
                            first = server.xin.get(timeout=20)
                            if first is not None:
                                items.append(first)
                            while len(items) < 32:
                                nxt = server.xin.get_nowait()
                                if nxt is not None:
                                    items.append(nxt)
                        except queue.Empty:
                            pass
                    if server._stopping:
                        return
                    self._send(200, json.dumps(items).encode("utf-8"))
                else:
                    self._send(404)

            def _local(self):
                if self.client_address[0] != "127.0.0.1":
                    return False
                for h in ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For"):
                    if self.headers.get(h) is not None:
                        return False
                return True

            def _handle_x(self, topic, parts):
                if server.ladder is None or not secrets.compare_digest(topic, server.ladder.topic):
                    self.close_connection = True
                    self._send(404, cors=True)
                    return
                with server._xlock:
                    blocked = time.monotonic() < server._x_block_until
                if blocked:
                    self.close_connection = True
                    self._send(429, cors=True)
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > 90 * 1024:
                    self.close_connection = True
                    _x_fail()
                    self._send(403, cors=True)
                    return
                if n > 0:
                    data = _read(self.rfile, n)
                    if data is None:
                        self.close_connection = True
                        return
                    s = data.decode("utf-8", "replace")
                    obj = server.ladder.open(s)
                    if obj is None:
                        _x_fail()
                        self._send(403, cors=True)
                        return
                    if obj.get("t") == "lpos":
                        ack = server.take_env(s, "tunnel")
                        if ack and server.xout is not None:
                            with server._xlock:
                                server._xout_seq += 1
                                server.xout.append((server._xout_seq, time.monotonic(), ack))
                    elif server.xin is not None:
                        server.xin.put(s)
                try:
                    a_cur = int(parse_qs(parts.query).get("a", ["0"])[0])
                except ValueError:
                    a_cur = 0
                with server._xlock:
                    now = time.monotonic()
                    msgs = [m for mid, mt, m in server.xout if mid > a_cur and now - mt < 60.0]
                    cur_out = server._xout_seq
                self._send(200, json.dumps({"a": cur_out, "m": msgs}).encode("utf-8"), cors=True)

            def do_POST(self):  # noqa: N802
                parts = urlsplit(self.path)
                bits = parts.path.strip("/").split("/")
                if len(bits) == 2 and bits[0] == "x":
                    self._handle_x(bits[1], parts)
                    return
                route, parts = self._route()
                if route == "bridge-event" and self._local():
                    try:
                        n = min(int(self.headers.get("Content-Length") or 0), 4096)
                        ev = json.loads(_read(self.rfile, n) or b"{}")
                    except ValueError:
                        self._send(400)
                        return
                    if isinstance(ev, dict) and isinstance(ev.get("type"), str):
                        server.events.put(("bridge", ev))
                    self._send(200, b'{"ok":true}')
                    return
                if route == "x-push" and self._local():
                    if server.xout is None:
                        self.close_connection = True
                        self._send(404)
                        return
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        n = -1
                    if n <= 0 or n > 90 * 1024:
                        self.close_connection = True
                        self._send(413 if n > 90 * 1024 else 400)
                        return
                    data = _read(self.rfile, n)
                    if data is None:
                        self.close_connection = True
                        return
                    with server._xlock:
                        server._xout_seq += 1
                        server.xout.append((server._xout_seq, time.monotonic(), data.decode("utf-8", "replace")))
                    self._send(200, b'{"ok":true}')
                    return
                if route == "lpos" and self._local():
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        n = -1
                    if n <= 0 or n > 90 * 1024:
                        self.close_connection = True
                        self._send(413 if n > 90 * 1024 else 400)
                        return
                    data = _read(self.rfile, n)
                    if data is None:
                        self.close_connection = True
                        return
                    via = parse_qs(parts.query).get("via", [""])[0]
                    ack = server.take_env(data.decode("utf-8", "replace"), via)
                    if ack:
                        self._send(200, ack.encode("utf-8"), "text/plain; charset=utf-8")
                    else:
                        self._send(204)
                    return
                if route == "drive-got" and self._local():   # the receiver page asks which originals are here
                    try:
                        n = min(int(self.headers.get("Content-Length") or 0), 32768)
                        body_in = json.loads(_read(self.rfile, n) or b"{}")
                        ids = body_in.get("ids")
                        for_clean = bool(body_in.get("for_clean"))
                    except (ValueError, AttributeError):
                        self._send(400)
                        return
                    ids = [i for i in ids if isinstance(i, str) and len(i) < 80][:200] if isinstance(ids, list) else []
                    self._send(200, json.dumps({"clean": server.drive_clean,
                                                "ids": server.store.originals(ids, for_drive_clean=for_clean)}).encode("utf-8"))
                    return
                if route == "copy-status":   # copy poll: LAN phones or the receiver page ask for [id, c, h8]
                    try:
                        n = min(int(self.headers.get("Content-Length") or 0), 32768)
                        ids = json.loads(_read(self.rfile, n) or b"{}").get("ids")
                    except (ValueError, AttributeError):
                        self._send(400)
                        return
                    ids = [i for i in ids if isinstance(i, str) and len(i) < 80][:100] if isinstance(ids, list) else []
                    self._send(200, json.dumps({"rt": int(time.time() * 1000),
                                                "items": server.store.copy_status(ids)}).encode("utf-8"))
                    return
                if route == "pos":
                    try:
                        n = min(int(self.headers.get("Content-Length") or 0), 2048)
                        pos = clean_pos(json.loads(_read(self.rfile, n) or b"{}"))
                    except ValueError:
                        pos = None
                    if pos is not None:
                        pos = server.store.resolve_pos(pos)
                    if pos is None:
                        self._send(400)
                        return
                    server.events.put(("pos", pos))
                    self._send(200, b'{"ok":true}')
                    return
                if route == "hs":
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        n = -1
                    if n <= 0 or n > 512:
                        self.close_connection = True
                        self._send(413 if n > 512 else 400)
                        return
                    raw_hs = _read(self.rfile, n)
                    if raw_hs is None:
                        self.close_connection = True
                        return
                    try:
                        hs = clean_hs(json.loads(raw_hs))
                    except ValueError:
                        hs = None
                    if hs is not None:
                        hs = server.store.resolve_pos(hs)
                    if hs is None:
                        self._send(400)
                        return
                    hs["d"] = hs["id"]
                    hs["n"] = hs["name"]
                    server.events.put(("hs", hs))
                    self._send(200, b'{"ok":true}')
                    return
                if route != "photo":
                    self._send(404)
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n <= 0 or n > MAX_PHOTO:
                    self.close_connection = True
                    self._send(413 if n > MAX_PHOTO else 400)
                    return
                data = _read(self.rfile, n)
                if data is None:
                    self.close_connection = True
                    return
                try:
                    raw_m = json.loads(parse_qs(parts.query).get("m", [""])[0])
                    if isinstance(raw_m, dict) and raw_m.get("sealed"):
                        # sealed for this computer: surveyor and device_id are read from inside the sealed payload only
                        ssha = hashlib.sha256(data).hexdigest()
                        meta, data = server.handover.open(data)
                        if isinstance(meta, dict) and meta.get("tu") == 1 and isinstance(raw_m.get("dt"), (int, float)):
                            meta = dict(meta, dt=raw_m["dt"])
                    else:
                        ssha = hashlib.sha256(data).hexdigest()
                        meta = raw_m
                    rec, new = server.store.save(meta, data, ssha=ssha)
                except BlockedDeviceError as b:
                    server.events.put(("rejected", {"device_id": b.device_id, "reason": b.reason}))
                    self._send(403, json.dumps({"ok": False, "error": "blocked", "reason": b.reason}).encode())
                    return
                except (ValueError, TypeError):
                    self._send(400)
                    return
                except OSError:
                    self._send(507)  # disk full / folder gone: the phone keeps the photo and retries
                    return
                if new:
                    server.events.put(("photo", rec))
                c_cnt, h_hex, _h8 = server.store.ledger.receipt_info(rec["id"])
                if not h_hex:
                    h_hex = ssha or rec.get("ssha") or rec.get("sha") or ""
                self._send(200, json.dumps({
                    "ok": True, "id": rec["id"], "c": c_cnt, "h": h_hex, "rt": int(time.time() * 1000)
                }).encode())

        self.httpd = ExclusiveServer((bind, port), Handler)
        self.httpd.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def set_ladder(self, key):
        """Enable the fallback ladder envelope handler and tunnel relay queues."""
        from .ladder import Ladder

        self.ladder = Ladder(key)
        self._rl_key = key
        self.refresh_recipients()
        self.xin = queue.Queue()
        self.xout = deque(maxlen=64)
        self.tunnel_host = ""
        self.org_host = ""
        self._xout_seq = 0
        self._x_fails = deque()
        self._x_block_until = 0.0

    def refresh_recipients(self):
        """Sign the recipients list again (after the office changes it) and send it with the hello and the guide."""
        from . import recipients

        key = getattr(self, "_rl_key", None)
        try:
            self.rl = recipients.signed(self.cert_folder, self.handover.public, key) if key else None
        except Exception:  # noqa: BLE001 - no list: phones keep sealing v1 (or for the list they already have)
            self.rl = None
        self._publish_guide()
        return self.rl

    def take_env(self, s, via):
        """Process a sealed ladder envelope; queue positions and ladder info, return sealed ack."""
        if self.ladder is None:
            return None
        mirror_ok = False
        mf = self.store.ledger.mirror_folder
        if mf:
            pos_id = "pos-" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:24]
            self.store.ledger.mark(pos_id, "office", ssha=hashlib.sha256(s.encode("utf-8")).hexdigest())
            if self.store.ledger.write_mirror_bytes(pos_id, os.path.join("positions", f"{pos_id}.lpos"), s.encode("utf-8")):
                mirror_ok = True
        positions, ack = self.ladder.take(s, via, clean_pos, self.store.resolve_pos, mirror_ok=mirror_ok)
        for pos in positions:
            self.events.put(("pos", pos))
        if self.ladder.last_info is not None:
            info = dict(self.ladder.last_info, via=via, at=time.time())
            hs = info.get("hs")
            if isinstance(hs, dict):
                hs_in = dict(hs)
                if not hs_in.get("route") and via in ("lan", "direct", "relay", "peer", "mqtt", "org", "own", "tunnel", "drive"):
                    hs_in["route"] = via
                hs_res = self.store.resolve_pos(hs_in)
                if hs_res is not None:
                    hs_res["d"] = hs_res["id"]
                    hs_res["n"] = hs_res["name"]
                    info["hs"] = hs_res
                    self.events.put(("hs", hs_res))
                else:
                    info.pop("hs", None)
            self.events.put(("ladder", info))
        return ack

    def _bridge_config_bytes(self):
        d = json.loads(self.bridge_config.decode("utf-8"))
        d["eod"] = self.eod_hour
        if getattr(self, "rl", None):
            d["rl"] = self.rl
        if self.ladder is not None:
            d["lad"] = 1
            d["tun"] = self.tunnel_host or ""
            d["org"] = self.org_host or ""
        return json.dumps(d, ensure_ascii=False).encode("utf-8")

    def set_guide(self, items):
        """Send these to every phone (they read it within seconds); returns how many went."""
        self._guide_items = list(items)
        return self._publish_guide()

    def set_indoor(self, payload):
        """The indoor map goes with the guide; None takes it away. Returns how many rooms went."""
        self._indoor = payload
        self._publish_guide()
        return len(payload["units"]) if payload else 0

    def set_capture(self, payload):
        """The capture schemas and open jobs go with the guide; None takes them away."""
        self._capture = payload
        self._publish_guide()
        return len(payload.get("jobs", ())) if payload else 0

    def _publish_guide(self):
        self.guide_v += 1
        self.guide, n = guide_bytes(self.guide_v, getattr(self, "_guide_items", []))
        indoor = getattr(self, "_indoor", None)
        cap = getattr(self, "_capture", None)
        rl = getattr(self, "rl", None)
        if indoor or cap or rl:
            g = json.loads(self.guide)
            if indoor:
                g["indoor"] = indoor
            if cap:
                g["cap"] = cap
            if rl:
                g["rl"] = rl   # carries its own signature: the guide is not signed
            self.guide = json.dumps(g, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return n

    def url(self, host):
        return f"https://{host}:{self.port}/{self.token}/"

    def path(self):
        return f"/{self.token}/"

    def stop(self):
        self._stopping = True
        if self.xin is not None:
            self.xin.put(None)
        self.httpd.shutdown()
        self.httpd.server_close()


def _read(stream, n):
    chunks, left = [], n
    while left > 0:
        b = stream.read(min(left, 1 << 20))
        if not b:
            return None
        chunks.append(b)
        left -= len(b)
    return b"".join(chunks)


def phone_texts(tr):
    """The phone page's texts in one language (tr: the translation of an English text)."""
    return {"title": tr("Biruni GIS - field photos"),
            "intro": tr("Allow the location when asked. Each photo appears on the computer's map at once and "
                        "is saved there. Keep this page open."),
            "start": tr("Start"), "waiting": tr("Waiting for the location…"), "shoot": tr("Take a photo"),
            "note": tr("Note (optional)"), "sent": tr("On the computer:"), "queued": tr("Waiting to send:"),
            "waitingPhotos": tr("{n} photos waiting"),
            "previewSent": tr("{n} arrived as preview, original waiting for Wi-Fi"),
            "relayMode": tr("Direct link blocked by the mobile network - sending encrypted previews over the relays; "
                            "full originals wait for Wi-Fi."),
            "incognito": tr("Private browsing is on: closing this page will erase waiting photos. Open the link in a "
                            "normal browser tab."),
            "connected": tr("Connected to the computer"),
            "via": tr("Connected through a teammate's phone."),
            "lost": tr("Not reaching the computer - photos are kept on the phone and sent later."),
            "denied": tr("Location not allowed."),
            "insecure": tr("This browser blocks the location here. Use Chrome or Safari and accept the "
                           "certificate warning."),
            # the bridge (bridge.py)
            "connecting": tr("Connecting to the computer… photos taken now are kept and sent."),
            "pcoff": tr("The computer is not answering. Is Biruni open, with Field photos started? Photos are "
                        "kept on the phone."),
            "p2pfail": tr("This network does not allow a direct link to the computer. Photos are kept on the "
                          "phone; try another network or Wi-Fi."),
            "nokey": tr("Open this page by scanning the code in Biruni GIS (Field photos)."),
            # the page's own labels
            "hero": tr("Field photos"), "position": tr("Position"), "direction": tr("Direction"),
            "kind": tr("Kind (optional)"), "recent": tr("Recent photos"), "language": tr("Language"),
            "online": tr("Connected"), "offline": tr("Not connected"), "linking": tr("Connecting…"),
            "kept": tr("Saved on the phone - sending…"), "none": tr("Your photos will appear here."),
            "arrived": tr("✓ Arrived on the computer ({n})"),
            # the form
            "yes": tr("Yes"), "no": tr("No"), "need": tr("Fill in the marked questions first."),
            "bad": tr("This value is not right."), "required": tr("Required"),
            # the team map, the guide and the averaged position
            "me": tr("Your name on the computer's map"), "share": tr("Show me on the computer's map"),
            "needName": tr("Write your name first so your photos carry it."),
            "blocked": tr("This phone has been blocked by the computer."),
            "guide": tr("Guide from the computer"), "inside": tr("Inside - {d} from the edge"),
            "outside": tr("Outside - {d} to the edge"), "near": tr("{d} from the line"),
            "target": tr("{d} to {name}"), "point": tr("the point"),
            "avg": tr("Average here (20 s)"), "stop": tr("Stop"),
            "averaging": tr("Averaging… {n} readings - keep the phone still"),
            "averaged": tr("Averaged from {n} readings, spread ± {s} m - used for the next photo"),
            # inside a building: the room code is the position
            "scanRoom": tr("Scan the room code"), "pickRoom": tr("Or choose the room"),
            "findRoom": tr("Room number or name"), "tapRoom": tr("Tap where you are in the room"),
            "level": tr("Level {n}"), "noRoom": tr("This code is not in the indoor map the computer sent."),
            "leaveRoom": tr("Outside the room"), "noCamera": tr("The camera cannot read codes here - choose the room."),
            # without internet: photos handed to a teammate's phone
            "hand": tr("Without internet"), "handGiveBtn": tr("Give my photos to a teammate"),
            "handTakeBtn": tr("Take a teammate's photos"), "handCancel": tr("Cancel"), "handWait": tr("Getting ready…"),
            "handGive1": tr("Let your teammate scan this code, then scan the code that appears on their phone."),
            "handTake1": tr("Scan the code on your teammate's phone."),
            "handTake2": tr("Now let your teammate scan this code."), "handLinking": tr("Linking the two phones…"),
            "handSending": tr("Handing over {n} of {k}…"), "handTaking": tr("{n} photos taken…"),
            "handGave": tr("{n} photos handed over. They stay on this phone too until one of you reaches the computer."),
            "handTook": tr("{n} photos taken. They go to the computer with yours; they are sealed for it, so this phone "
                           "cannot open them."),
            "handNone": tr("No photos are waiting to be handed over."),
            "handNoKey": tr("This phone has not met the computer yet, so it cannot seal its photos for it."),
            "handNoScan": tr("This browser cannot read codes. Use Chrome on Android."),
            "handFail": tr("The two phones could not link. Put both on the same Wi-Fi or on one phone's hotspot - no "
                           "internet is needed."),
            "handBad": tr("This is not a hand-over code."), "handCarry": tr("Carrying {n} photos for teammates."),
            # sending through the surveyor's own Google Drive (drive_page.py)
            "driveTitle": tr("Google Drive"), "driveLink": tr("Link Google Drive"),
            "driveRelink": tr("Link Google Drive again"), "driveSend": tr("Send photos ({n})"), "driveFree": tr("Free space in Google Drive ({n})"), "driveOpen": tr("Open the Google Drive link"),
            "driveUnlinked": tr("Link Google Drive and every photo goes to the office's Google Drive folder, locked so "
                                "that only the office computer can open it."),
            "driveIdle": tr("Linked. Every photo goes to the office's Google Drive folder. When the computer is in "
                            "reach, it shows a small copy at once."),
            "driveGps": tr("Linked. When the computer cannot be reached any other way, positions go through Google "
                           "Drive, up to a minute late."),
            "drivePreview": tr("{n} shown on the computer, original going to Google Drive"),
            "driveAuth": tr("The link to Google Drive has ended. Tap to link again."),
            "driveUp": tr("Sending to Google Drive…"), "driveWait": tr("Google Drive is busy - trying again soon."),
            "driveFull": tr("Your Google Drive is full. Photos wait on this phone until the computer is in reach."),
            "driveErr": tr("Google Drive could not be linked."), "driveDenied": tr("Google Drive was not allowed."),
            "driveInApp": tr("To use Google Drive, open this page in Chrome or Safari."),
            "drivePopupBlocked": tr("The browser blocked the Google Drive window. Allow pop-ups for this page."),
            "driveIn": tr("In Google Drive:"),
            "driveMode": tr("The computer cannot be reached - photos are going to the office's Google Drive folder."),
            "drivePick": tr("Choose the office folder"),
            "drivePickHint": tr("The office shared a folder named {name} with your Google account. Choose it once."),
            "drivePickWrong": tr("That is not the office's folder. Choose the one named {name}."),
            "drivePickRO": tr("The folder is shared for viewing only. Ask the office to make you an Editor."),
            "driveGone": tr("The office folder can no longer be reached. Ask the office to share it again, then choose it."),
            "driveOwn": tr("in your own Google Drive. If the office uses another account, choose the folder it shared."),
            "drivePickAgain": tr("Choose another folder"), "driveFolder": tr("Folder:"),
            # fallback ladder (ladder_page.py)
            "ladKeep": tr("{n} positions kept on the phone - they are sent when the computer is in reach."),
            "ladCatch": tr("Sending {n} earlier positions…"),
            "ladVia": tr("Sending through: {name}"),
            "chTunnel": tr("direct address"),
            "chRelay": tr("public relay"),
            "chOrg": tr("your organization's relay"),
            "chOwn": tr("Biruni relay"),
            # capture jobs and checklist (phase C)
            "capJobs": tr("Capture job"), "capNoJob": tr("No job (free photos)"),
            "capDoneOf": tr("{done} of {total}"), "capAdd": tr("+ Add {name}"),
            "capRemove": tr("Remove"), "capMinWarn": tr("At least {n} required"),
            "capMaxWarn": tr("At most {n} allowed"), "capTake": tr("Take a photo"),
            "capTakeN": tr("Take a photo ({have}/{min})"), "capPhotosN": tr("{have}/{min} photos"),
            "capExtra": tr("Extra photo for this job"), "capCannot": tr("Cannot do this item"),
            "capReason": tr("Reason (at least 3 characters)"), "capReasonNeed": tr("Write a reason of at least 3 characters."),
            "capClearSkip": tr("Clear skip"), "capSkipped": tr("Skipped: {r}"),
            "capFinish": tr("Finish job"), "capReopen": tr("Reopen job"),
            "capClosed": tr("Job finished on this phone"), "capMissingTitle": tr("Missing required items:"),
            "capMissingNeed": tr("Complete every required item or give a reason before finishing."),
            "capNeedPhotos": tr("needs {n} more photo(s)"), "capNeedQ": tr("missing required answers"),
            "capNeedInst": tr("needs {have}/{min}"), "capDone": tr("Done"), "capOpt": tr("Optional"),
            # no-loss item states, copy counts & end-of-day reminder (phase P1)
            "stPhone": tr("Saved on the phone"),
            "stCloud": tr("☁️ Reached the cloud, waiting for the office"),
            "stOffice": tr("Reached the office (1 copy)"),
            "stSafe": tr("✓✓ Protected (2 copies)"),
            "stRefused": tr("Refused by the office"),
            "stRefusedWhy": tr("Refused: {r}"),
            "countCloud": tr("☁️ {n} in the cloud, waiting for the office"),
            "countOffice": tr("{n} at the office, waiting for 2nd copy"),
            "countSafe": tr("✓✓ {n} protected (2 copies)"),
            "countRefused": tr("{n} refused — kept on this phone"),
            "eodTitle": tr("Items not yet protected by 2 copies"),
            "eodBody": tr("Keep this page open or connect to Wi-Fi so every item reaches 2 verified copies."),
            "eodPending": tr("{n} items still waiting for 2 copies"),
            "eodDismiss": tr("Keep open & send now"),
            # iOS install gate, persistent storage, unsent banner & sealed export (phase P2)
            "iosGateTitle": tr("Install to Home Screen first"),
            "iosGateBody": tr("Safari on iPhone and iPad erases browser storage after 7 days. Add this page to your Home Screen before taking photos so your work is never lost."),
            "iosStep1": tr("1. Tap the Share button in Safari."),
            "iosStep2": tr("2. Choose “Add to Home Screen”, then tap Add."),
            "iosCopyLink": tr("Copy link"),
            "iosCopied": tr("Link copied. Open the installed app from your Home Screen."),
            "iosPasteTitle": tr("Paste the office link"),
            "iosPasteBody": tr("Paste the link you copied from Safari (or scan the office QR code again) to connect this installed app."),
            "iosPasteHint": tr("https://photos.biruni-gis.com/p/#k=…"),
            "iosPasteUse": tr("Use link"),
            "iosPasteBad": tr("That link is not a valid Biruni field photos link."),
            "androidInstall": tr("Install app to Home Screen for safer offline storage"),
            "androidInstallBtn": tr("Install"),
            "persistWarn": tr("Browser storage is not marked persistent. Keep this page open or export a backup bundle before closing."),
            "spaceFree": tr("Free space on phone: {s}"),
            "quotaStop": tr("Phone storage is full. Capture is stopped so queued items are not lost — connect to the office or export a backup bundle."),
            "unsentBanner": tr("{n} items not protected for {d} days"),
            "exportTitle": tr("Sealed backup bundle"),
            "exportBtn": tr("Export sealed backup (.bfx)"),
            "exporting": tr("Packing sealed backup…"),
            "exportDone": tr("Exported {n} sealed items ({f} file(s)). Nothing was deleted from this phone."),
            "exportEmpty": tr("No sealed items are waiting on this phone.")}


def all_phone_texts():
    """The phone page's texts in every language Biruni has, so the page can switch."""
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "i18n")
    out = {"en": phone_texts(lambda s: s)}
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else ():
        if name.endswith(".json"):
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    table = json.load(f)
            except (OSError, ValueError):
                continue
            out[name[:-5]] = phone_texts(lambda s, t=table: t.get(s) or s)
    return out


def render_page(texts, lang="en", kinds=(), form=(), pckey=""):
    """texts: {language: phone_texts(...)}; lang: the language the page opens in (the phone can switch)."""
    kinds = [k for k in (s.strip() for s in kinds) if k][:24]
    return (PAGE.replace("__LANG__", lang.replace("_", "-")).replace("__DEFLANG__", lang)
            .replace("__DIR__", "rtl" if lang in ("ar", "fa") else "ltr")
            .replace("__TEXTS__", json.dumps(texts, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__KINDS__", json.dumps(kinds, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__FORM__", json.dumps(clean_form(list(form)), ensure_ascii=False).replace("</", "<\\/"))
            .replace("__PCKEY__", pckey or "")).encode("utf-8")


PAGE = r"""<!doctype html><html lang="__LANG__" dir="__DIR__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#F4F6F8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0E1114" media="(prefers-color-scheme: dark)">
<meta name="color-scheme" content="light dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icon.png">
<link rel="apple-touch-icon" href="icon.png"><title>Biruni GIS</title>
<style>
:root{--bg:#F4F6F8;--card:#FFFFFF;--text:#14181D;--dim:#5F6B76;--line:#E1E6EB;--accent:#12A594;--ink:#FFFFFF;
 --soft:#E1F4F1;--good:#12A36E;--fair:#C98A12;--poor:#D24B4B;--shadow:0 1px 2px rgba(16,24,32,.05),0 6px 20px rgba(16,24,32,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#0E1114;--card:#171B20;--text:#E8ECEF;--dim:#97A3AE;--line:#262C33;
 --accent:#2CC5B4;--ink:#05201C;--soft:#11302C;--good:#3DD39A;--fair:#E8B04B;--poor:#F07070;--shadow:none}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--text)}
body{font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans Arabic","Noto Sans",Tahoma,sans-serif;
 min-height:100vh;-webkit-font-smoothing:antialiased}
body.working{padding-bottom:calc(118px + env(safe-area-inset-bottom))}
svg{width:1em;height:1em;flex:none}
button{font:inherit;cursor:pointer}
.top{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:10px;
 padding:calc(10px + env(safe-area-inset-top)) 16px 10px;background:var(--bg)}
.brand{display:flex;flex:none;align-items:center;gap:9px;font-weight:700;font-size:17px;letter-spacing:-.01em}
.logo{width:30px;height:30px;border-radius:10px;background:var(--accent);color:var(--ink);display:grid;place-items:center;font-size:18px}
.tools{display:flex;gap:8px;align-items:center;min-width:0}
#waitPill{display:block;min-width:0;overflow:hidden;text-overflow:ellipsis}   /* a long count never widens the page */
.pill{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;padding:6px 11px;border-radius:999px;
 background:var(--card);border:1px solid var(--line);color:var(--dim);white-space:nowrap}
.pill.warn{border-color:var(--fair);color:var(--fair);font-weight:700}
.pill svg{font-size:15px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}
.dot.on{background:var(--good)}.dot.off{background:var(--poor)}.dot.wait{background:var(--fair);animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.3}}
.banner{margin:2px 16px 4px;padding:10px 13px;border-radius:14px;font-size:14px;line-height:1.45;
 background:var(--card);border:1px solid var(--line);border-inline-start:4px solid var(--fair)}
.banner.amber{border-color:var(--fair);border-inline-start-color:var(--fair);color:var(--fair);font-weight:700}
.banner.red{border-color:var(--poor);border-inline-start-color:var(--poor);color:var(--poor);font-weight:700}
.banner.row{display:flex;align-items:center;justify-content:space-between;gap:10px}
.igate{text-align:start;margin-top:14px}
.igate h2{font-size:20px;margin:0 0 8px}
.igate p{color:var(--dim);font-size:14px;margin:0 0 12px}
.istep{display:flex;align-items:center;gap:12px;padding:10px 12px;border-radius:14px;background:var(--bg);
 border:1px solid var(--line);margin-bottom:10px;font-weight:600;font-size:15px}
.iico{width:38px;height:38px;border-radius:10px;background:var(--soft);color:var(--accent);display:grid;
 place-items:center;font-size:20px;flex:none}
.dlbox{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.dlbox a{display:inline-flex;align-items:center;gap:6px;padding:8px 12px;border-radius:10px;background:var(--soft);
 color:var(--accent);font-weight:600;font-size:13px;text-decoration:none}
main{max-width:520px;margin:0 auto;padding:10px 16px 16px}
.intro{text-align:center;padding:26px 6px 10px}
.hero{position:relative;width:118px;height:118px;margin:10px auto 22px;border-radius:34px;background:var(--soft);
 color:var(--accent);display:grid;place-items:center;font-size:54px}
.hero .pin{position:absolute;inset-inline-end:-8px;top:-8px;width:44px;height:44px;border-radius:50%;background:var(--accent);
 color:var(--ink);display:grid;place-items:center;font-size:24px;border:4px solid var(--bg)}
h1{font-size:27px;line-height:1.2;margin:0 0 10px;letter-spacing:-.02em}
.intro p{color:var(--dim);margin:0 auto 26px;max-width:32ch}
.primary{width:100%;padding:16px;border:0;border-radius:16px;background:var(--accent);color:var(--ink);font-size:17px;
 font-weight:700;box-shadow:var(--shadow);transition:transform .12s}
.primary:active{transform:scale(.98)}.primary:disabled{opacity:.45}
.card{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:15px;margin-bottom:12px;box-shadow:var(--shadow)}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px}
.lbl{font-size:13px;font-weight:600;color:var(--dim)}
.coords{display:block;font:600 21px/1.3 ui-monospace,"SF Mono",Menlo,Consolas,monospace;margin:6px 0 12px;
 font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.meter{height:6px;border-radius:6px;background:var(--line);overflow:hidden}
.meter i{display:block;height:100%;width:6%;border-radius:6px;background:var(--dim);transition:width .6s,background .6s}
.acc{font-weight:700;font-variant-numeric:tabular-nums;direction:ltr;unicode-bidi:isolate}
.good{color:var(--good)}.fair{color:var(--fair)}.poor{color:var(--poor)}
.meter i.good{background:var(--good)}.meter i.fair{background:var(--fair)}.meter i.poor{background:var(--poor)}
.sub{margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
.dir{display:inline-flex;align-items:center;gap:7px;font-weight:700;font-variant-numeric:tabular-nums}
.dir svg{font-size:18px;color:var(--accent);transition:transform .35s}
.kinds{display:flex;gap:8px;overflow-x:auto;margin:10px -15px 12px;padding:0 15px;scrollbar-width:none}
.kinds::-webkit-scrollbar{display:none}
.kinds button{flex:none;font-size:15px;font-weight:600;padding:8px 15px;border-radius:999px;border:1px solid var(--line);
 background:var(--bg);color:var(--text);transition:background .15s,color .15s,border-color .15s}
.kinds button.sel{background:var(--accent);border-color:var(--accent);color:var(--ink)}
input.in{width:100%;font:inherit;padding:12px 14px;border-radius:13px;border:1px solid var(--line);
 background:var(--bg);color:var(--text);outline:none;transition:border-color .15s;min-height:48px}
input.in:focus{border-color:var(--accent)}
.fld{margin-bottom:14px}.fld .lbl{display:block;margin-bottom:7px}.fld .kinds{flex-wrap:wrap;overflow:visible;margin:0;padding:0}
.req{color:var(--poor);margin-inline-start:3px}
.fld.bad .lbl,.card.bad .lbl{color:var(--poor)}.fld.bad input.in,.card.bad input.in{border-color:var(--poor)}
.fld.bad .kinds button:not(.sel){border-color:var(--poor)}
@keyframes shake{25%{transform:translateX(-5px)}75%{transform:translateX(5px)}}.fld.bad,.card.bad{animation:shake .3s}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:12px 15px;box-shadow:var(--shadow)}
.stat b{display:block;font-size:27px;line-height:1.15;font-variant-numeric:tabular-nums}
.stat span{font-size:13px;color:var(--dim)}.stat.warn b{color:var(--fair)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:9px}
.grid div{position:relative;aspect-ratio:1;border-radius:15px;overflow:hidden;background:var(--line);animation:pop .35s ease-out}
@keyframes pop{from{transform:scale(.86);opacity:0}}
.grid img,.last img{width:100%;height:100%;object-fit:cover;display:block}
.badge{position:absolute;bottom:6px;inset-inline-end:6px;min-width:24px;height:24px;padding:0 6px;display:grid;
 place-items:center;border-radius:12px;font-size:13px;font-weight:700;background:rgba(0,0,0,.62);color:#fff}
.badge.ok{background:var(--good)}.badge.bad{background:var(--poor)}.badge.cloud{background:var(--fair)}
.empty{grid-column:1/-1;aspect-ratio:auto!important;animation:none!important;background:transparent!important;
 color:var(--dim);font-size:14px;text-align:center;padding:14px 0}
.shutterbar{position:fixed;inset-inline:0;bottom:0;z-index:6;display:grid;grid-template-columns:1fr auto 1fr;align-items:center;
 gap:12px;padding:12px 22px calc(12px + env(safe-area-inset-bottom));background:var(--card);border-top:1px solid var(--line)}
.last{width:54px;height:54px;border-radius:15px;overflow:hidden;background:var(--line);justify-self:start}
.shutter{width:80px;height:80px;border:0;padding:0;border-radius:50%;background:transparent;position:relative}
.shutter .ring{position:absolute;inset:0;border-radius:50%;border:4px solid var(--accent)}
.shutter .core{position:absolute;inset:9px;border-radius:50%;background:var(--accent);color:var(--ink);display:grid;
 place-items:center;font-size:28px;transition:transform .12s}
.shutter:active .core{transform:scale(.9)}
.shutter:disabled .ring{border-color:var(--line);border-top-color:var(--accent);animation:spin 1s linear infinite}
.shutter:disabled .core{background:var(--line);color:var(--dim)}
@keyframes spin{to{transform:rotate(360deg)}}
.hint{justify-self:end;font-size:13px;line-height:1.35;color:var(--dim);text-align:end;max-width:118px}
.sheet{position:fixed;inset:0;z-index:10;background:rgba(0,0,0,.42);display:flex;align-items:flex-end;animation:fade .2s}
.panel{width:100%;max-width:520px;margin:0 auto;background:var(--card);border-radius:24px 24px 0 0;
 padding:10px 12px calc(14px + env(safe-area-inset-bottom));animation:up .25s ease-out}
.grab{width:40px;height:5px;border-radius:3px;background:var(--line);margin:0 auto 10px}
.panel .lbl{padding:0 12px 6px}
.panel button{display:flex;justify-content:space-between;align-items:center;width:100%;font-size:17px;padding:13px 12px;
 border:0;border-radius:13px;background:transparent;color:var(--text);text-align:start}
.panel button.sel{background:var(--soft);font-weight:700}.panel button.sel::after{content:"✓";color:var(--accent)}
@keyframes up{from{transform:translateY(48px);opacity:0}}@keyframes fade{from{opacity:0}}
.toast{position:fixed;left:50%;bottom:calc(126px + env(safe-area-inset-bottom));z-index:8;transform:translate(-50%,16px);
 opacity:0;background:var(--text);color:var(--bg);padding:10px 18px;border-radius:999px;font-size:14px;font-weight:600;
 transition:opacity .25s,transform .25s;pointer-events:none;white-space:nowrap;max-width:90vw;overflow:hidden;text-overflow:ellipsis}
.toast.show{opacity:1;transform:translate(-50%,0)}
.ghost{font-size:14px;font-weight:600;padding:8px 13px;border-radius:12px;border:1px solid var(--accent);
 background:transparent;color:var(--accent)}
.ghost.on{background:var(--accent);color:var(--ink)}
.avgInfo{font-size:13px;color:var(--dim);text-align:end;flex:1}
#avgBar{margin-top:10px}
.mrow{display:flex;gap:10px;align-items:center;margin-top:8px}
.sw{position:relative;flex:none;width:50px;height:30px}
.sw input{opacity:0;width:0;height:0;position:absolute}
.sw span{position:absolute;inset:0;border-radius:15px;background:var(--line);transition:background .2s}
.sw span::after{content:"";position:absolute;top:3px;inset-inline-start:3px;width:24px;height:24px;border-radius:50%;
 background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.25);transition:transform .2s}
.sw input:checked+span{background:var(--accent)}
.sw input:checked+span::after{transform:translateX(20px)}
[dir=rtl] .sw input:checked+span::after{transform:translateX(-20px)}
.sw input:focus-visible+span{outline:2px solid var(--accent);outline-offset:2px}
#gmap{display:block;width:100%;height:190px;margin:10px 0 8px;border-radius:14px;background:var(--bg)}
.gtext{font-size:17px;font-weight:700}
.gtext.in{color:var(--good)}.gtext.out{color:var(--fair)}
.coords.inroom{font:700 19px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans Arabic",Tahoma,sans-serif;letter-spacing:0}
.roomrow{display:flex;gap:8px;align-items:center;margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
.roomrow .ghost{flex:none}.roomName{flex:1;font-weight:700;font-size:15px}
#rmap{display:block;width:100%;height:220px;margin:10px 0 4px;border-radius:14px;background:var(--bg);touch-action:none}
.scan{position:relative;width:100%;aspect-ratio:1;border-radius:16px;overflow:hidden;background:#000;margin-bottom:10px}
.scan video{width:100%;height:100%;object-fit:cover}
.scan i{position:absolute;inset:18%;border:3px solid rgba(255,255,255,.85);border-radius:18px}
.rooms{max-height:40vh;overflow:auto}
.handInfo{font-size:13px;color:var(--dim);margin-top:8px}.handInfo:empty{display:none}
.handStep{font-size:16px;font-weight:600;line-height:1.45;padding:2px 12px 12px;text-align:center}
#handQr{display:block;width:min(78vw,300px);height:auto;margin:0 auto 12px;border-radius:12px;image-rendering:pixelated}
#handScan{max-width:260px;margin:0 auto 12px}.panel button#handCancel{justify-content:center}
.hide{display:none!important}
.dframe{display:block;width:100%;height:84px;border:0;margin-top:8px;background:transparent}
.dstate{font-size:14px;color:var(--dim);margin-top:4px}.dcount{font-weight:700;font-variant-numeric:tabular-nums}
.dpop,.dpick{margin-top:8px}.dfold{font-size:13px;color:var(--dim);margin-top:4px;overflow-wrap:anywhere}
.csel{width:100%;font:inherit;font-weight:600;padding:10px 12px;border-radius:13px;border:1px solid var(--line);
 background:var(--bg);color:var(--text);margin:8px 0 10px;min-height:44px}
.cprog{font-size:13px;font-weight:700;color:var(--dim);font-variant-numeric:tabular-nums}
.ctree{margin-top:10px;display:flex;flex-direction:column;gap:6px}
.cnode{border:1px solid var(--line);border-radius:13px;padding:9px 11px;background:var(--bg)}
.cnode.sub{margin-inline-start:14px}
.crow{display:flex;align-items:center;justify-content:space-between;gap:8px}
.cbtn{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;border:0;padding:2px 0;
 background:transparent;color:var(--text);text-align:start;font-weight:600;font-size:15px}
.cmeta{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--dim);flex:none}
.cst{font-weight:700;font-size:15px}.cst.ok{color:var(--good)}.cst.no{color:var(--poor)}.cst.sk{color:var(--fair)}
.cadd{margin-top:6px;width:100%;text-align:center}.cdel{padding:4px 9px;font-size:13px;border-color:var(--line);color:var(--dim)}
.cfin{margin-top:12px;width:100%}.cmiss{margin-top:10px;padding:10px 12px;border-radius:12px;border:1px solid var(--poor);
 background:var(--bg);font-size:13px;color:var(--poor)}.cmiss ul{margin:6px 0 0;padding-inline-start:18px}
.cpanel{max-height:86vh;overflow-y:auto}.cpath{font-weight:700;font-size:16px;padding:2px 4px 10px}
.cbox{margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<header class="top">
 <div class="brand"><span class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="11" r="3"/><path d="M17.66 16.66L13.41 20.9a2 2 0 0 1-2.83 0l-4.24-4.24a8 8 0 1 1 11.32 0z"/></svg></span><span id="title">Biruni GIS</span></div>
 <div class="tools"><span class="pill warn hide" id="waitPill"></span><span class="pill" id="chip"><i class="dot wait" id="dot"></i><span id="chipText"></span></span>
 <button class="pill" id="langBtn" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3.6 9h16.8M3.6 15h16.8M11.5 3a17 17 0 0 0 0 18M12.5 3a17 17 0 0 1 0 18"/></svg><span id="langCode"></span></button></div>
</header>
<div class="banner hide" id="st"></div>
<div class="banner hide" id="privWarn"></div>
<div class="banner hide" id="persistWarn"></div>
<div class="banner hide" id="quotaWarn"></div>
<div class="banner hide" id="unsentWarn"></div>
<div class="banner row hide" id="androidBar"><span id="androidText"></span><button class="ghost" id="androidBtn" type="button"></button></div>
<main>
<section class="card igate hide" id="iosGate">
 <h2 id="iosGateTitle"></h2>
 <p id="iosGateBody"></p>
 <div class="istep"><span class="iico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M8 7l4-4 4 4"/><path d="M5 11v8a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-8"/></svg></span><span id="iosStep1"></span></div>
 <div class="istep"><span class="iico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="4"/><path d="M12 8v8M8 12h8"/></svg></span><span id="iosStep2"></span></div>
 <button class="primary" id="iosCopyBtn" type="button"></button>
</section>
<section class="card igate hide" id="iosPaste">
 <h2 id="iosPasteTitle"></h2>
 <p id="iosPasteBody"></p>
 <div class="mrow"><input type="url" class="in" id="iosPasteIn" dir="ltr" autocomplete="off"><button class="ghost" id="iosPasteBtn" type="button"></button></div>
 <div class="dstate" id="iosPasteErr"></div>
</section>
<section id="intro" class="intro">
 <div class="hero"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5 7h1a2 2 0 0 0 2-2a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1a2 2 0 0 0 2 2h1a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2"/><circle cx="12" cy="13" r="3"/></svg>
 <span class="pin"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="11" r="3"/><path d="M17.66 16.66L13.41 20.9a2 2 0 0 1-2.83 0l-4.24-4.24a8 8 0 1 1 11.32 0z"/></svg></span></div>
 <h1 id="hero"></h1><p id="introText"></p><button class="primary" id="go" type="button"></button>
</section>
<section id="work" class="hide">
 <div class="card hide" id="guideCard">
  <div class="row"><span class="lbl" id="lGuide"></span><span class="dir"><svg id="garrow" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l7.5 19L12 16.8 4.5 21z"/></svg></span></div>
  <canvas id="gmap"></canvas>
  <div class="gtext" id="gtext">—</div>
 </div>
 <div class="card">
  <div class="row"><span class="lbl" id="lPos"></span><span class="acc" id="acc"></span></div>
  <bdi class="coords" id="pos">—</bdi>
  <div class="meter"><i id="accBar"></i></div>
  <div class="row sub"><span class="lbl" id="lDir"></span><span class="dir"><svg id="arrow" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l7.5 19L12 16.8 4.5 21z"/></svg><bdi id="deg">—</bdi></span></div>
  <div class="row sub"><button class="ghost" id="avgBtn" type="button"></button><span class="avgInfo" id="avgInfo"></span></div>
  <div class="meter hide" id="avgBar"><i id="avgFill"></i></div>
  <div class="roomrow hide" id="roomRow"><button class="ghost" id="roomBtn" type="button"></button>
   <span class="roomName" id="roomName"></span><button class="ghost hide" id="roomOff" type="button">×</button></div>
  <div class="hide" id="roomPlan"><canvas id="rmap"></canvas><span class="lbl" id="roomHint"></span></div>
 </div>
 <div class="card hide" id="capCard">
  <div class="row"><span class="lbl" id="lCap"></span><span class="cprog" id="capProg"></span></div>
  <select class="csel" id="capJob"></select>
  <div class="ctree" id="capTree"></div>
  <div class="cmiss hide" id="capMiss"></div>
  <button class="ghost cfin" id="capFin" type="button"></button>
 </div>
 <div class="card" id="formCard">
  <span class="lbl" id="lKind"></span><div class="kinds" id="kinds"></div>
  <div id="form"></div>
  <input type="text" class="in" id="note" enterkeyhint="done" autocomplete="off">
 </div>
 <div class="card" id="meCard"><label class="lbl" for="me" id="lMe"></label>
  <div class="mrow"><input type="text" class="in" id="me" maxlength="40" autocomplete="name">
   <label class="sw"><input type="checkbox" id="share" checked><span></span></label></div></div>
 <div class="stats"><div class="stat"><b id="nSent">0</b><span id="lSent"></span></div>
  <div class="stat" id="waitBox"><b id="nWait">0</b><span id="lWait"></span></div></div>
 <div class="card" id="handCard"><span class="lbl" id="lHand"></span>
  <div class="mrow"><button class="ghost" id="giveBtn" type="button"></button><button class="ghost" id="takeBtn" type="button"></button></div>
  <div class="handInfo" id="handInfo"></div></div>
 <div class="card" id="exportCard"><div class="row"><span class="lbl" id="lExport"></span><span class="lbl" id="spaceInfo"></span></div>
  <div class="mrow"><button class="ghost" id="exportBtn" type="button"></button></div>
  <div class="dstate" id="exportInfo"></div>
  <div class="dlbox" id="exportLinks"></div></div>
 <span class="lbl" id="lRecent"></span><div id="list" class="grid"></div>
</section>
</main>
<footer class="shutterbar hide" id="bar"><div class="last" id="last"></div>
 <button id="shot" class="shutter" type="button" disabled><span class="ring"></span><span class="core"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 7h1a2 2 0 0 0 2-2a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1a2 2 0 0 0 2 2h1a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2"/><circle cx="12" cy="13" r="3"/></svg></span></button>
 <div class="hint" id="shotHint"></div></footer>
<input id="file" type="file" accept="image/*" capture="environment" class="hide">
<div class="sheet hide" id="sheet"><div class="panel"><div class="grab"></div><div class="lbl" id="lLang"></div><div id="langs"></div></div></div>
<div class="sheet hide" id="scanSheet"><div class="panel"><div class="grab"></div>
 <div class="scan" id="scanBox"><video id="scanVideo" playsinline muted></video><i></i></div>
 <div class="lbl" id="lPick"></div><input type="text" class="in" id="roomFind" autocomplete="off">
 <div class="rooms" id="roomList"></div></div></div>
<div class="sheet hide" id="handSheet"><div class="panel"><div class="grab"></div>
 <div class="handStep" id="handStep" aria-live="polite"></div><canvas id="handQr" class="hide"></canvas>
 <div class="scan hide" id="handScan"><video id="handVideo" playsinline muted></video><i></i></div>
 <button type="button" id="handCancel"></button></div></div>
<div class="sheet hide" id="capSheet"><div class="panel cpanel"><div class="grab"></div>
 <div class="cpath" id="capPath"></div>
 <div id="capForm"></div>
 <div class="fld"><span class="lbl" id="lCapNote"></span><input type="text" class="in" id="capNote" enterkeyhint="done" autocomplete="off"></div>
 <button class="primary" id="capShoot" type="button"></button>
 <div class="cbox"><span class="lbl" id="lCapSkip"></span>
  <div class="mrow"><input type="text" class="in" id="capReason" maxlength="300" autocomplete="off">
   <button class="ghost" id="capSkipBtn" type="button"></button></div></div>
 <button type="button" id="capDoneBtn"></button></div></div>
<div class="sheet hide" id="eodSheet"><div class="panel"><div class="grab"></div>
 <div class="cpath" id="eodTitle"></div>
 <div class="dstate" id="eodBody"></div>
 <div class="dstate" id="eodList"></div>
 <button class="primary cfin" id="eodBtn" type="button"></button></div></div>
<div class="toast" id="toast"></div>
<script>
let ALL=__TEXTS__,KINDS=__KINDS__,FORM=__FORM__,ROOM='',HASHL=null,GPSMODE=false,T={},LANG='en',PCKEY='__PCKEY__';const DEF='__DEFLANG__',$=i=>document.getElementById(i);
const NAMES={en:'English',ar:'العربية',es:'Español',pt_BR:'Português',fr:'Français',tr:'Türkçe',fa:'فارسی'};
function pick(l){if(!l)return null;l=String(l).replace('-','_');if(ALL[l])return l;const b=l.split('_')[0].toLowerCase();
 return Object.keys(ALL).find(k=>k.split('_')[0].toLowerCase()===b)||null}
function keep(k,v){try{if(v==null)localStorage.removeItem(k);else localStorage.setItem(k,v)}catch(e){}}
function kept(k){try{return localStorage.getItem(k)}catch(e){return null}}
var SKEW=0,SKEW_OK=false;
function NOW(){return Date.now()+SKEW}
function noteTime(ms){if(typeof ms==='number'&&isFinite(ms)&&ms>1700000000000){SKEW=Math.round(ms-Date.now());SKEW_OK=true}}
async function probeSkew(){try{const r=await fetch('sw.js',{method:'HEAD',cache:'no-store'});
 const d=r&&r.headers&&r.headers.get('Date');if(d){const t=Date.parse(d);if(isFinite(t))noteTime(t)}}catch(e){}}
/*BOOT*/
let NL=kept('biruni-nl-'+ROOM)==='1',EOD_HOUR=parseInt(kept('biruni-eod-'+ROOM)||'18',10);if(!(EOD_HOUR>=0&&EOD_HOUR<=23))EOD_HOUR=18;
function setNl(on){if(on){NL=true;keep('biruni-nl-'+ROOM,'1')}}
function setEodHour(h){if(typeof h==='number'&&h>=0&&h<=23){EOD_HOUR=Math.trunc(h);keep('biruni-eod-'+ROOM,String(EOD_HOUR))}}
const _HP=new URLSearchParams(typeof location!=='undefined'?location.hash.slice(1):'');
const _SK=_HP.get('sk')||'',_SN=_HP.get('sn')||'';
if(_SK){keep('biruni-sk-'+ROOM,_SK);if(_SN){keep('biruni-sn-'+ROOM,_SN);keep('biruni-me',_SN)}}
else if(_HP.get('k')||_HP.get('shared')){keep('biruni-sk-'+ROOM,null);keep('biruni-sn-'+ROOM,null)}
const SURVKEY=_SK||kept('biruni-sk-'+ROOM)||'',SURVNAME=_SN||kept('biruni-sn-'+ROOM)||'';
if(!PCKEY||PCKEY.charAt(0)==='_')PCKEY=kept('biruni-pckey-'+ROOM)||'';else keep('biruni-pckey-'+ROOM,PCKEY);
let fix=null,compass=null,kind='',sent=0,db=null,mem=[],busy=false,pending=null,lastSt=null,started=false,privMode=false;
const dev=((navigator.userAgentData&&navigator.userAgentData.platform)||navigator.platform||'').slice(0,40);
const bare=s=>String(s||'').replace(/\s*[:：]\s*$/,'');
function label(){document.title=T.title;$('hero').textContent=T.hero;$('introText').textContent=T.intro;
 $('go').textContent=T.start;$('note').placeholder=T.note;$('lPos').textContent=T.position;$('lDir').textContent=T.direction;
 $('lKind').textContent=T.kind;$('lRecent').textContent=T.recent;$('lLang').textContent=T.language;
 $('lGuide').textContent=T.guide;$('lMe').textContent=T.me;
 {const r=document.createElement('span');r.className='req';r.textContent='*';r.title=T.required||'';$('lMe').appendChild(r)}
 $('share').setAttribute('aria-label',T.share);
 $('me').placeholder=T.me;$('roomBtn').textContent=T.scanRoom;$('roomOff').setAttribute('aria-label',T.leaveRoom);if(room)$('roomHint').textContent=T.tapRoom;if(!avg)$('avgBtn').textContent=T.avg;
 $('lHand').textContent=T.hand;$('giveBtn').textContent=T.handGiveBtn;$('takeBtn').textContent=T.handTakeBtn;
 $('handCancel').textContent=T.handCancel;$('lSent').textContent=bare(T.sent);$('lWait').textContent=bare(T.queued);$('shotHint').textContent=shotFix()?T.shoot:T.waiting;
 $('shot').setAttribute('aria-label',T.shoot);$('langBtn').setAttribute('aria-label',T.language);
 $('langCode').textContent=LANG.split('_')[0].toUpperCase();const e=$('list').querySelector('.empty');if(e)e.textContent=T.none;
 if(privMode&&$('privWarn'))$('privWarn').textContent=T.incognito||'';
 if($('eodTitle'))$('eodTitle').textContent=T.eodTitle||'Items not yet protected by 2 copies';
 if($('eodBody'))$('eodBody').textContent=T.eodBody||'Keep this page open or connect to Wi-Fi so every item reaches 2 verified copies.';
 if($('eodBtn'))$('eodBtn').textContent=T.eodDismiss||'Keep open & send now';
 if($('iosGateTitle'))$('iosGateTitle').textContent=T.iosGateTitle||'Install to Home Screen first';
 if($('iosGateBody'))$('iosGateBody').textContent=T.iosGateBody||'Safari on iPhone and iPad erases browser storage after 7 days. Add this page to your Home Screen before taking photos so your work is never lost.';
 if($('iosStep1'))$('iosStep1').textContent=T.iosStep1||'1. Tap the Share button in Safari.';
 if($('iosStep2'))$('iosStep2').textContent=T.iosStep2||'2. Choose “Add to Home Screen”, then tap Add.';
 if($('iosCopyBtn'))$('iosCopyBtn').textContent=T.iosCopyLink||'Copy link';
 if($('iosPasteTitle'))$('iosPasteTitle').textContent=T.iosPasteTitle||'Paste the office link';
 if($('iosPasteBody'))$('iosPasteBody').textContent=T.iosPasteBody||'Paste the link you copied from Safari (or scan the office QR code again) to connect this installed app.';
 if($('iosPasteIn'))$('iosPasteIn').placeholder=T.iosPasteHint||'https://photos.biruni-gis.com/p/#k=…';
 if($('iosPasteBtn'))$('iosPasteBtn').textContent=T.iosPasteUse||'Use link';
 if($('androidText'))$('androidText').textContent=T.androidInstall||'Install app to Home Screen for safer offline storage';
 if($('androidBtn'))$('androidBtn').textContent=T.androidInstallBtn||'Install';
 if($('lExport'))$('lExport').textContent=T.exportTitle||'Sealed backup bundle';
 if($('exportBtn'))$('exportBtn').textContent=T.exportBtn||'Export sealed backup (.bfx)';
 if(typeof updatePersistBanner==='function')updatePersistBanner();
 if(typeof updateQuotaBanner==='function')updateQuotaBanner();
 if(typeof updateSpaceInfo==='function')updateSpaceInfo();
 if(typeof count==='function')count();
 if(typeof driveLabel==='function')driveLabel();if(typeof capLabel==='function')capLabel()}
function setLang(l){LANG=l;T=Object.assign({},ALL.en||{},ALL[l]||{});const h=document.documentElement;
 h.lang=l.replace('_','-');h.dir=(l==='ar'||l==='fa')?'rtl':'ltr';label();drawKinds();drawForm();if(typeof capDraw==='function')capDraw();
 if(lastSt)status(lastSt[0],lastSt[1]);else chip('wait')}
function uid(){const r=crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
 return r.replace(/[^A-Za-z0-9-]/g,'')}
function drawKinds(){$('kinds').textContent='';KINDS.forEach(k=>{const b=document.createElement('button');b.type='button';
 b.textContent=k;b.classList.toggle('sel',k===kind);b.onclick=()=>{kind=kind===k?'':k;
 [...$('kinds').children].forEach(x=>x.classList.toggle('sel',x.textContent===kind))};$('kinds').appendChild(b)});
 $('kinds').classList.toggle('hide',!KINDS.length);$('lKind').classList.toggle('hide',!KINDS.length)}
/* the form designed on the PC: the answers go with the photo; kept ones stay for the next photo */
let vals={};try{vals=JSON.parse(kept('biruni-vals-'+ROOM)||'{}')||{}}catch(e){}
const saveVals=()=>keep('biruni-vals-'+ROOM,JSON.stringify(vals));
const digits=s=>String(s).replace(/[٠-٩]/g,d=>d.charCodeAt(0)-1632).replace(/[۰-۹]/g,d=>d.charCodeAt(0)-1776)
 .replace(/[٫,]/g,'.').replace(/[−–]/g,'-').trim();
function chips(f,box,vmap,onch){vmap=vmap||vals;onch=onch||saveVals;
 const multi=f.type==='multi',opts=f.type==='yesno'?[[true,T.yes],[false,T.no]]:(f.choices||[]).map(c=>[c,c]);
 const on=v=>multi?(vmap[f.name]||[]).includes(v):vmap[f.name]===v;
 opts.forEach(([v,t])=>{const b=document.createElement('button');b.type='button';b.textContent=t;b.classList.toggle('sel',on(v));
  b.onclick=()=>{if(multi){const a=(vmap[f.name]||[]).filter(x=>x!==v);if(!on(v))a.push(v);vmap[f.name]=a}
   else vmap[f.name]=on(v)?undefined:v;if(vmap[f.name]===undefined||(multi&&!vmap[f.name].length))delete vmap[f.name];
   [...box.children].forEach((x,i)=>x.classList.toggle('sel',on(opts[i][0])));box.parentNode.classList.remove('bad');onch(f.name,vmap[f.name])};
  box.appendChild(b)})}
function drawFormIn(box,form,vmap,onch){if(!box)return;box.textContent='';(form||[]).forEach(f=>{const d=document.createElement('div');d.className='fld';
 d.dataset.name=f.name;const l=document.createElement('span');l.className='lbl';l.textContent=f.label;
 if(f.req){const r=document.createElement('span');r.className='req';r.textContent='*';r.title=T.required;l.appendChild(r)}d.appendChild(l);
 if(['choice','multi','yesno'].includes(f.type)){const c=document.createElement('div');c.className='kinds';chips(f,c,vmap,onch);d.appendChild(c)}
 else{const i=document.createElement('input');i.className='in';i.autocomplete='off';i.enterKeyHint='next';
  i.type=f.type==='date'?'date':'text';if(f.type==='number')i.inputMode='decimal';if(f.type==='integer')i.inputMode='numeric';if(f.type!=='text')i.dir='ltr';
  if(f.type==='text')i.maxLength=250;
  if(f.min!=null||f.max!=null)i.placeholder=(f.min!=null?f.min:'')+' – '+(f.max!=null?f.max:'');
  i.value=vmap[f.name]!=null?vmap[f.name]:'';i.oninput=()=>{if(i.value==='')delete vmap[f.name];else vmap[f.name]=i.value;
   d.classList.remove('bad');onch(f.name,vmap[f.name])};d.appendChild(i)}
 box.appendChild(d)})}
function drawForm(){drawFormIn($('form'),FORM,vals,saveVals)}
/* checked before the camera opens: null and the field marked when something is missing or wrong */
function checkForm(form,vmap,box){const out={};let first=null,why='';const root=box||document;
 for(const f of form||[]){let v=vmap[f.name],ok=true;
  if(v==null||v===''||(Array.isArray(v)&&!v.length)){if(f.req){ok=false;why=why||'need'}}
  else if(f.type==='number'||f.type==='integer'){const n=Number(digits(v));
   if(digits(v)===''||!isFinite(n)||(f.type==='integer'&&!Number.isInteger(n))||(f.min!=null&&n<f.min)||(f.max!=null&&n>f.max)){
    ok=false;why=why||'bad'}else out[f.name]=n}
  else if(f.type==='text')out[f.name]=String(v).trim().slice(0,250);
  else out[f.name]=v;
  const e=root.querySelector('.fld[data-name="'+f.name+'"]');
  if(e){e.classList.remove('bad');if(!ok){void e.offsetWidth;e.classList.add('bad');first=first||e}}}
 if(first){first.scrollIntoView({behavior:'smooth',block:'center'});toast(T[why]);if(navigator.vibrate)navigator.vibrate([40,60,40]);return null}
 return out}
function answers(){return checkForm(FORM,vals,$('form'))}
function afterShot(){FORM.forEach(f=>{if(!f.keep)delete vals[f.name]});saveVals();drawForm()}
/* distances on the ground: a flat local frame in metres around (lat0, lon0) is exact enough for a few km */
function proj(lat0,lon0){const k=Math.cos(lat0*Math.PI/180);return (lon,lat)=>[(lon-lon0)*k*111320,(lat-lat0)*110540]}
function meters(a,b){const P=proj(a.lat,a.lon),v=P(b.lon,b.lat);return Math.hypot(v[0],v[1])}
/* a distance stays left to right inside Arabic text (LRI ... PDI) */
function fmtD(m){return '\u2066'+(m<1000?(m<10?m.toFixed(1):Math.round(m))+' m':(m/1000).toFixed(2)+' km')+'\u2069'}
/* the team: this phone's position goes to the PC's map, with the name written here (it can be turned off) */
const DEVID=kept('biruni-dev')||(()=>{const d=uid().replace(/-/g,'').slice(0,16);keep('biruni-dev',d);return d})();
let lastPos=null;
/* GPSMODE (opened from the PC's GPS panel): every new position goes at once, at most once a second */
async function tellPos(force){const f=room&&indoorFix?indoorFix:fix;if(!f||(!GPSMODE&&!$('share').checked))return;
 const now=Date.now();
 if(!force&&lastPos&&(GPSMODE?now-lastPos.at<1000:now-lastPos.at<15000&&meters(lastPos,f)<10))return;
 lastPos={lat:f.lat,lon:f.lon,at:now};
 try{await sendPos(Object.assign({id:DEVID,name:(SURVNAME||$('me').value).trim().slice(0,40),
  sk:SURVKEY||undefined,lat:f.lat,lon:f.lon,acc:f.acc,alt:f.alt,speed:f.speed,
  heading:compass!=null?compass:f.heading,cog:f.heading,t:f.t,level:f.level,unit:f.unit},
  netInfo()),force)}catch(e){}}
/* for the computer's list of the team: Wi-Fi or mobile data (Android tells it, iPhone does not), the battery, and
   how long the last message took to reach the computer and come back (the browser gives no signal bars) */
let PING=null,BAT=null;
function netInfo(){const c=navigator.connection||{};return {net:c.type||'',eff:c.effectiveType||'',down:c.downlink,
 crtt:c.rtt,ping:PING,bat:BAT?BAT.l:null,chg:BAT?BAT.c:null}}
try{navigator.getBattery().then(b=>{const u=()=>{BAT={l:b.level,c:b.charging}};u();b.onlevelchange=u;
 b.onchargingchange=()=>{u();tellPos(true)}}).catch(()=>{})}catch(e){}
try{navigator.connection.addEventListener('change',()=>tellPos(true))}catch(e){}
/* the guide: points to reach, lines and boundaries to walk, sent by the PC */
let GUIDE={v:0,items:[]};
function setGuide(g){if(!g||!Array.isArray(g.items))return;GUIDE=g;INDOOR=g.indoor&&Array.isArray(g.indoor.units)?g.indoor:null;
 showIndoor();if(room){const u=unitOf(room.id);if(u)room=u;else setRoom(null)}roomFromStore();
 if('cap' in g)capSet(g.cap);
 if(g.rl&&typeof rlTake==='function')rlTake(g.rl).then(ok=>{if(ok)sealWaiting()});
 try{const j=JSON.stringify(g);if(j.length<250000)keep('biruni-guide-'+ROOM,j)}catch(e){}
 $('guideCard').classList.toggle('hide',!g.items.length);drawGuide();if(room)drawRoom()}
function segDist(p,a,b){const dx=b[0]-a[0],dy=b[1]-a[1],l=dx*dx+dy*dy;let t=l?((p[0]-a[0])*dx+(p[1]-a[1])*dy)/l:0;
 t=Math.max(0,Math.min(1,t));const x=a[0]+t*dx,y=a[1]+t*dy;return [Math.hypot(p[0]-x,p[1]-y),x,y]}
function inRing(p,r){let c=false;for(let i=0,j=r.length-1;i<r.length;j=i++){const a=r[i],b=r[j];
 if((a[1]>p[1])!==(b[1]>p[1])&&p[0]<(b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0])c=!c}return c}
/* the nearest thing to the phone: {d metres, x/y of the nearest place, item, inside} */
function nearest(P){let best=null;const me=[0,0];
 for(const it of GUIDE.items){let d=Infinity,x=0,y=0,ins=false;
  if(it.k==='pt'){[x,y]=P(it.c[0],it.c[1]);d=Math.hypot(x,y)}
  else{const parts=it.c.map(part=>part.map(c=>P(c[0],c[1])));
   for(const part of parts){const n=part.length,closed=it.k==='poly';
    for(let i=0;i<n-1+(closed?1:0);i++){const r=segDist(me,part[i],part[(i+1)%n]);if(r[0]<d)[d,x,y]=r}}
   if(it.k==='poly')ins=parts.reduce((c,r)=>c!==inRing(me,r),false)}
  const rank=ins?-1e9+d:d;if(!best||rank<best.rank)best={d:d,x:x,y:y,it:it,inside:ins,rank:rank}}
 return best}
function drawGuide(){const cv=$('gmap');if($('guideCard').classList.contains('hide'))return;
 const W=cv.clientWidth||300,H=cv.clientHeight||190,dpr=window.devicePixelRatio||1;
 if(cv.width!==Math.round(W*dpr)){cv.width=Math.round(W*dpr);cv.height=Math.round(H*dpr)}
 const g=cv.getContext('2d'),css=getComputedStyle(document.documentElement),col=n=>css.getPropertyValue(n).trim();
 g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,W,H);
 const c0=fix||(()=>{const it=GUIDE.items[0],c=it.k==='pt'?it.c:it.c[0][0];return {lat:c[1],lon:c[0]}})();
 const P=proj(c0.lat,c0.lon),shapes=GUIDE.items.map(it=>({it:it,xy:it.k==='pt'?[[P(it.c[0],it.c[1])]]:
  it.c.map(part=>part.map(c=>P(c[0],c[1])))}));
 let x0=0,y0=0,x1=0,y1=0;shapes.forEach(s=>s.xy.forEach(part=>part.forEach(([x,y])=>{x0=Math.min(x0,x);x1=Math.max(x1,x);
  y0=Math.min(y0,y);y1=Math.max(y1,y)})));
 if(!fix){x0=x1=y0=y1=NaN;shapes.forEach(s=>s.xy.forEach(part=>part.forEach(([x,y])=>{x0=isNaN(x0)?x:Math.min(x0,x);
  x1=isNaN(x1)?x:Math.max(x1,x);y0=isNaN(y0)?y:Math.min(y0,y);y1=isNaN(y1)?y:Math.max(y1,y)})))}
 const pad=18,span=Math.max(x1-x0,y1-y0,20),k=Math.min((W-2*pad)/Math.max(x1-x0,span*.2),(H-2*pad)/Math.max(y1-y0,span*.2));
 const ox=W/2-k*(x0+x1)/2,oy=H/2+k*(y0+y1)/2,S=([x,y])=>[ox+k*x,oy-k*y];
 const acc=col('--accent');g.lineJoin='round';g.lineCap='round';
 shapes.forEach(s=>{if(s.it.k==='poly'){g.beginPath();s.xy.forEach(r=>r.forEach((p,i)=>{const[a,b]=S(p);i?g.lineTo(a,b):g.moveTo(a,b)}));
   g.closePath();g.globalAlpha=.16;g.fillStyle=acc;g.fill('evenodd');g.globalAlpha=1;g.strokeStyle=acc;g.lineWidth=2;g.stroke()}
  else if(s.it.k==='line'){g.strokeStyle=acc;g.lineWidth=3;s.xy.forEach(part=>{g.beginPath();
   part.forEach((p,i)=>{const[a,b]=S(p);i?g.lineTo(a,b):g.moveTo(a,b)});g.stroke()})}
  else{const[a,b]=S(s.xy[0][0]);g.fillStyle=acc;g.beginPath();g.arc(a,b,6,0,7);g.fill();
   if(s.it.n){g.fillStyle=col('--text');g.font='600 12px system-ui,sans-serif';g.textAlign='center';g.fillText(s.it.n,a,b-10)}}});
 const t=$('gtext');if(!fix){t.textContent=T.waiting;t.className='gtext';return}
 const n=nearest(P);if(!n){t.textContent='—';return}
 const[mx,my]=S([0,0]),[nx,ny]=S([n.x,n.y]);g.setLineDash([5,5]);g.strokeStyle=col('--dim');g.lineWidth=1.5;g.beginPath();
 g.moveTo(mx,my);g.lineTo(nx,ny);g.stroke();g.setLineDash([]);
 g.fillStyle='rgba(43,127,255,.18)';g.beginPath();g.arc(mx,my,Math.max(8,Math.min(60,k*(fix.acc||0))),0,7);g.fill();
 g.fillStyle='#2B7FFF';g.strokeStyle='#fff';g.lineWidth=3;g.beginPath();g.arc(mx,my,7,0,7);g.fill();g.stroke();
 const name=n.it.n||T.point,d=fmtD(n.d);
 t.textContent=n.it.k==='poly'?(n.it.n?n.it.n+': ':'')+(n.inside?T.inside:T.outside).replace('{d}',d):
  n.it.k==='line'?(n.it.n?n.it.n+': ':'')+T.near.replace('{d}',d):T.target.replace('{d}',d).replace('{name}',name);
 t.className='gtext'+(n.it.k==='poly'?(n.inside?' in':' out'):'');
 const brg=Math.atan2(n.x,n.y)*180/Math.PI,heading=compass!=null?compass:0;
 $('garrow').style.transform='rotate('+Math.round(brg-heading)+'deg)';$('garrow').parentNode.classList.toggle('hide',n.d<1)}
window.addEventListener('resize',()=>drawGuide());
/* inside a building the GPS cannot help: the code on the room's door says where the phone is,
   and a tap on the room's plan says where in the room */
let INDOOR=null,room=null,indoorFix=null,scanStream=null,scanTimer=null,rplan=null;
const unitOf=id=>INDOOR&&INDOOR.units.find(u=>u.id===id);
function levelName(lv){const l=INDOOR&&INDOOR.levels.find(x=>x.id===lv);return l&&l.name?l.name:T.level.replace('{n}',lv)}
function roomLabel(u){return [u.num,u.n].filter(Boolean).join(' · ')||u.id}
function showIndoor(){const on=!!(INDOOR&&INDOOR.units.length);$('roomRow').classList.toggle('hide',!on);
 if(!on&&room)setRoom(null);$('roomBtn').textContent=T.scanRoom}
function setRoom(u){room=u||null;$('roomOff').classList.toggle('hide',!room);$('roomPlan').classList.toggle('hide',!room);
 if(!room){indoorFix=null;$('roomName').textContent='';if(!fix){const sf=shotFix();$('shot').disabled=!sf;$('shotHint').textContent=sf?T.shoot:T.waiting;
  $('pos').classList.remove('inroom');$('pos').textContent='—';$('acc').textContent=''}
  else showFix();return}
 indoorFix={lat:u.p[1],lon:u.p[0],acc:u.r,alt:null,heading:null,t:Date.now(),level:u.lv,unit:u.id};
 $('roomName').textContent=roomLabel(u)+' · '+levelName(u.lv);$('roomHint').textContent=T.tapRoom;
 $('pos').textContent=roomLabel(u)+' · '+levelName(u.lv);$('pos').classList.add('inroom');$('acc').textContent='± '+Math.round(u.r)+' m';
 $('shot').disabled=false;$('shotHint').textContent=T.shoot;keep('biruni-room-'+ROOM,u.id);
 if(navigator.vibrate)navigator.vibrate(20);drawRoom()}
function roomFromText(t){t=String(t||'').trim();let id=t;const m=t.match(/[#&]u=([^&]+)/);if(m)id=decodeURIComponent(m[1]);
 const u=unitOf(id);if(!u){toast(T.noRoom);return false}setRoom(u);return true}
/* the room's plan: the room and its neighbours on its level; a tap moves the position inside it */
function drawRoom(){if(!room)return;const cv=$('rmap'),W=cv.clientWidth||300,H=cv.clientHeight||220,dpr=window.devicePixelRatio||1;
 if(cv.width!==Math.round(W*dpr)){cv.width=Math.round(W*dpr);cv.height=Math.round(H*dpr)}
 const g=cv.getContext('2d'),css=getComputedStyle(document.documentElement),col=n=>css.getPropertyValue(n).trim();
 g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,W,H);const P=proj(room.p[1],room.p[0]);
 const rr=room.c.map(r=>r.map(c=>P(c[0],c[1])));let x0=1e9,y0=1e9,x1=-1e9,y1=-1e9;
 rr.forEach(r=>r.forEach(([x,y])=>{x0=Math.min(x0,x);x1=Math.max(x1,x);y0=Math.min(y0,y);y1=Math.max(y1,y)}));
 const m=Math.max(x1-x0,y1-y0)*.35+2;x0-=m;x1+=m;y0-=m;y1+=m;
 const k=Math.min(W/(x1-x0),H/(y1-y0)),ox=W/2-k*(x0+x1)/2,oy=H/2+k*(y0+y1)/2,S=([x,y])=>[ox+k*x,oy-k*y];
 rplan={P:P,k:k,ox:ox,oy:oy,W:W,H:H};
 const path=rings=>{g.beginPath();rings.forEach(r=>r.forEach((p,i)=>{const[a,b]=S(p);i?g.lineTo(a,b):g.moveTo(a,b)}));g.closePath()};
 INDOOR.units.filter(u=>u.lv===room.lv&&u.id!==room.id).forEach(u=>{const rs=u.c.map(r=>r.map(c=>P(c[0],c[1])));
  let a0=1e9,b0=1e9,a1=-1e9,b1=-1e9;rs[0].forEach(([x,y])=>{a0=Math.min(a0,x);a1=Math.max(a1,x);b0=Math.min(b0,y);b1=Math.max(b1,y)});
  if(a1<x0||a0>x1||b1<y0||b0>y1)return;path(rs);g.strokeStyle=col('--line');g.lineWidth=1.5;g.stroke();
  const[a,b]=S(P(u.p[0],u.p[1]));if(u.num){g.fillStyle=col('--dim');g.font='12px system-ui,sans-serif';g.textAlign='center';
   g.fillText(u.num,a,b+4)}});
 path(rr);g.globalAlpha=.18;g.fillStyle=col('--accent');g.fill('evenodd');g.globalAlpha=1;g.strokeStyle=col('--accent');
 g.lineWidth=2.5;g.stroke();
 const[mx,my]=S(P(indoorFix.lon,indoorFix.lat));g.fillStyle='#2B7FFF';g.strokeStyle='#fff';g.lineWidth=3;g.beginPath();
 g.arc(mx,my,7,0,7);g.fill();g.stroke()}
/* px, py: the tap on the drawn plan, in its own pixels */
function tapRoom(px,py){if(!room||!rplan)return;const x=(px-rplan.ox)/rplan.k,y=(rplan.oy-py)/rplan.k;
 const lat=room.p[1]+y/110540,lon=room.p[0]+x/(Math.cos(room.p[1]*Math.PI/180)*111320);
 indoorFix=Object.assign({},indoorFix,{lat:lat,lon:lon,acc:1.5,t:Date.now()});$('acc').textContent='± 1.5 m';drawRoom()}
$('rmap').addEventListener('click',e=>{if(!rplan)return;const b=$('rmap').getBoundingClientRect();if(!b.width)return;
 tapRoom((e.clientX-b.left)*rplan.W/b.width,(e.clientY-b.top)*rplan.H/b.height)});
/* the scanner: the phone's own code reader where the browser has one, the list everywhere */
function roomList(){const q=$('roomFind').value.trim().toLowerCase(),box=$('roomList');box.textContent='';
 (INDOOR?INDOOR.units:[]).filter(u=>u.cat!=='corridor'&&(!q||(u.num+' '+u.n+' '+u.id).toLowerCase().includes(q)))
  .slice(0,60).forEach(u=>{const b=document.createElement('button');b.type='button';
   b.textContent=roomLabel(u)+' · '+levelName(u.lv);b.onclick=()=>{closeScan();setRoom(u)};box.appendChild(b)})}
async function openScan(){$('lPick').textContent=T.pickRoom;$('roomFind').placeholder=T.findRoom;$('roomFind').value='';
 roomList();$('scanSheet').classList.remove('hide');const can='BarcodeDetector' in window&&navigator.mediaDevices;
 $('scanBox').classList.toggle('hide',!can);if(!can){toast(T.noCamera);return}
 try{const det=new BarcodeDetector({formats:['qr_code']});
  scanStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}});
  const v=$('scanVideo');v.srcObject=scanStream;await v.play();
  scanTimer=setInterval(async()=>{try{const c=await det.detect(v);if(c.length&&roomFromText(c[0].rawValue))closeScan()}
   catch(e){}},350)}catch(e){$('scanBox').classList.add('hide');toast(T.noCamera)}}
function closeScan(){clearInterval(scanTimer);scanTimer=null;if(scanStream){scanStream.getTracks().forEach(t=>t.stop());
 scanStream=null}$('scanSheet').classList.add('hide')}
/* a room code read by the phone's camera app opens the room page (same site): it leaves the room here */
function roomFromStore(){try{const r=JSON.parse(kept('biruni-room')||'null');
 if(r&&r.u&&Date.now()-r.t<180000&&INDOOR){keep('biruni-room',null);roomFromText(r.u)}}catch(e){}}
window.addEventListener('storage',e=>{if(e.key==='biruni-room')roomFromStore()});
/* the averaged position: many readings while the phone stays still, odd ones left out */
const AVG_MS=20000;let avg=null,avgFix=null;
function average(pts){const accs=pts.map(p=>p.acc).sort((a,b)=>a-b),med=accs[accs.length>>1];
 let use=pts.filter(p=>p.acc<=2*med);
 const mean=a=>{let w=0,x=0,y=0;a.forEach(p=>{const k=1/Math.max(p.acc,1)**2;w+=k;x+=k*p.lon;y+=k*p.lat});return {lon:x/w,lat:y/w}};
 let m=mean(use);const P=proj(m.lat,m.lon),d=use.map(p=>Math.hypot(...P(p.lon,p.lat)));
 const rms=Math.sqrt(d.reduce((s,v)=>s+v*v,0)/d.length)||0,kept=use.filter((p,i)=>d[i]<=Math.max(2.5*rms,.5));
 if(kept.length>=3){use=kept;m=mean(use)}
 const P2=proj(m.lat,m.lon),sd=Math.sqrt(use.reduce((s,p)=>{const v=P2(p.lon,p.lat);return s+v[0]*v[0]+v[1]*v[1]},0)/use.length);
 const ua=use.map(p=>p.acc).sort((a,b)=>a-b);
 return {lat:m.lat,lon:m.lon,n:use.length,sd:sd,acc:ua[ua.length>>1]}}
function tickAvg(){if(!avg)return;const n=avg.pts.length,f=Math.min(1,(Date.now()-avg.t0)/AVG_MS);
 $('avgFill').style.width=Math.max(4,f*100)+'%';$('avgInfo').textContent=T.averaging.replace('{n}',n);
 if(f>=1&&n>=3){const r=average(avg.pts);avg=null;$('avgBar').classList.add('hide');$('avgBtn').classList.remove('on');
  $('avgBtn').textContent=T.avg;avgFix={lat:r.lat,lon:r.lon,acc:r.acc,alt:fix&&fix.alt,heading:fix&&fix.heading,
   t:Date.now(),n:r.n,sd:r.sd};$('avgInfo').textContent=T.averaged.replace('{n}',r.n).replace('{s}','\u2066'+r.sd.toFixed(1)+'\u2069');
  if(navigator.vibrate)navigator.vibrate([30,50,30])}}
function toggleAvg(){if(avg){avg=null;$('avgBar').classList.add('hide');$('avgBtn').classList.remove('on');
  $('avgBtn').textContent=T.avg;$('avgInfo').textContent='';return}
 avg={pts:[],t0:Date.now()};avgFix=null;$('avgBar').classList.remove('hide');$('avgBtn').classList.add('on');
 $('avgBtn').textContent=T.stop;tickAvg()}
setInterval(tickAvg,500);
/* the averaged position is used when it is recent and the phone has not moved away from it */
function shotFix(){if(room&&indoorFix)return indoorFix;if(avgFix&&fix&&Date.now()-avgFix.t<120000&&meters(avgFix,fix)<Math.max(15,2*(fix.acc||0))){
  const a=avgFix;avgFix=null;$('avgInfo').textContent='';return a}if(fix)return fix;
 if(typeof capFallbackFix==='function')return capFallbackFix();return null}
/* photos not yet on the PC wait in the phone's browser storage, sealed with the PC's key (ECDH P-256 + AES-GCM) */
/* a photo is kept at 5 MB at most (Hatem's choice): one plain upload carries it to Drive and the line sends it in
   one go; a bigger one is saved again as JPEG, first only a little more compressed, then smaller, until it fits.
   PMAX leaves room for the seal around it. The browser canvas holds about 16 million pixels at most (iPhone) */
const PMAX=5*1024*1024-65536,PPIX=16000000;
/* the camera's EXIF block (make, model, exposure, lens...) is carried over to the photo saved again; its
   Orientation is set to 1, the pixels being drawn upright already */
function jpegExif(u){if(!u||u[0]!==0xff||u[1]!==0xd8)return null;let i=2;
 while(i+10<=u.length&&u[i]===0xff){const m=u[i+1],n=(u[i+2]<<8)|u[i+3];if(m===0xda||m===0xd9)break;
  if(m===0xe1&&u[i+4]===0x45&&u[i+5]===0x78&&u[i+6]===0x69&&u[i+7]===0x66&&u[i+8]===0&&u[i+9]===0)
   return i+2+n<=u.length?u.slice(i,i+2+n):null;
  i+=2+n}return null}
function exifUpright(s){try{const t=10,le=s[t]===0x49,g16=o=>le?s[o]|(s[o+1]<<8):(s[o]<<8)|s[o+1],
  g32=o=>(le?(s[o]|(s[o+1]<<8)|(s[o+2]<<16)|(s[o+3]<<24)):((s[o]<<24)|(s[o+1]<<16)|(s[o+2]<<8)|s[o+3]))>>>0,
  ifd=t+g32(t+4);if(ifd+2>s.length)return s;const n=g16(ifd);
 for(let k=0;k<n;k++){const e=ifd+2+k*12;if(e+12>s.length)break;
  if(g16(e)===0x0112){if(le){s[e+8]=1;s[e+9]=0}else{s[e+8]=0;s[e+9]=1}}}}catch(e){}return s}
async function withExif(b,ex){if(!ex)return b;const u=new Uint8Array(await b.arrayBuffer());if(u[0]!==0xff||u[1]!==0xd8)return b;
 let at=2;if(u[2]===0xff&&u[3]===0xe0)at=4+((u[4]<<8)|u[5]);   /* after the JFIF block, which comes first */
 return new Blob([u.subarray(0,at),ex,u.subarray(at)],{type:'image/jpeg'})}
async function fitPhoto(blob){if(!blob||blob.size<=PMAX||typeof createImageBitmap!=='function')return blob;
 let ex=null;try{ex=jpegExif(new Uint8Array(await blob.slice(0,262144).arrayBuffer()));if(ex)ex=exifUpright(ex)}catch(e){}
 const room=PMAX-(ex?ex.length:0);
 let bm;try{bm=await createImageBitmap(blob);const w=bm.width,h=bm.height,s0=Math.min(1,Math.sqrt(PPIX/Math.max(1,w*h)));
  for(const [s,q] of [[1,.92],[1,.85],[.85,.85],[.7,.85],[.55,.82],[.4,.8],[.3,.75]]){
   const c=document.createElement('canvas');c.width=Math.max(1,Math.round(w*s*s0));c.height=Math.max(1,Math.round(h*s*s0));
   c.getContext('2d').drawImage(bm,0,0,c.width,c.height);
   const b=await new Promise(r=>c.toBlob(r,'image/jpeg',q));c.width=c.height=0;
   if(b&&b.size>0&&b.size<=room)return await withExif(b,ex)}}catch(e){}finally{if(bm&&bm.close)bm.close()}
 return blob}
async function makePreview(blob){
 if(typeof createImageBitmap==='function'){
  try{const bm=await createImageBitmap(blob),m=Math.max(bm.width,bm.height,1),s=m>720?720/m:1,
    c=document.createElement('canvas');c.width=Math.max(1,Math.round(bm.width*s));c.height=Math.max(1,Math.round(bm.height*s));
   c.getContext('2d').drawImage(bm,0,0,c.width,c.height);if(bm.close)bm.close();
   const pb=await new Promise(r=>c.toBlob(r,'image/jpeg',0.65));if(pb&&pb.size>0&&pb.size<blob.size)return pb}catch(e){}}
 const raw=new Uint8Array(await blob.arrayBuffer()),n=Math.min(raw.length,Math.max(512,raw.length>>2));
 if(n>=raw.length)return blob;const out=raw.slice(0,n);
 if(raw[0]===0xff&&raw[1]===0xd8){out[n-2]=0xff;out[n-1]=0xd9}
 return new Blob([out],{type:blob.type||'image/jpeg'})}
async function blobSha(b){if(!b||typeof b.arrayBuffer!=='function')return '';
 const u=new Uint8Array(await crypto.subtle.digest('SHA-256',await b.arrayBuffer()));
 let s='';for(let i=0;i<u.length;i++)s+=u[i].toString(16).padStart(2,'0');return s}
function itemSt(x){return (x&&typeof x.st==='number')?x.st:0}
function badgeFor(x){const st=itemSt(x);return st===3?'✓✓':st===2?'✓':st===1?'☁️':st===9?'✗':(x&&x.prevSent?'◐':'⏳')}
async function sealRecord(it){if(!it)return it;
 const st=itemSt(it),c=typeof it.c==='number'?it.c:0,at=it.at||NOW(),tu=it.tu||(it.meta&&it.meta.tu)||(!SKEW_OK?1:0);
 if(it.meta&&it.meta.sealed){
  if(!it.h&&it.blob)it.h=await blobSha(it.blob);
  it.st=st;it.c=c;it.at=at;if(tu){it.tu=1;it.meta.tu=1}return it}
 if(!PCKEY||!it.meta||!it.blob)return it;
 try{if(tu)it.meta.tu=1;
  const orig=await sealItem({meta:it.meta,blob:it.blob}),h=await blobSha(orig);let prev=null;
  if(!it.carried&&!it.prevSent&&st<1){const pb=it.cap?it.blob:await makePreview(it.blob);
   prev=await sealItem({meta:Object.assign({},it.meta,{preview:true}),blob:pb})}
  const r={id:it.id,blob:orig,prev:prev,prevSent:!!it.prevSent,meta:{id:it.id,sealed:1,t:it.meta.t||Date.now()},
   room:it.room||ROOM,carried:!!it.carried,pk:PCKEY,st:st,c:c,h:it.h||h,at:at};
  if(tu){r.tu=1;r.meta.tu=1;r.t0=it.t0||it.meta.t}
  if(it.why)r.why=it.why;
  if(it.cap){r.cap=1;if(it.csj)r.csj=it.csj}return r}catch(e){return it}}   /* pk: the key it is sealed for (.bfp kid) */
function openDb(){return new Promise(r=>{try{const q=indexedDB.open('biruni-photos',1);
 q.onupgradeneeded=()=>q.result.createObjectStore('q',{keyPath:'id'});q.onsuccess=()=>r(q.result);
 q.onerror=()=>r(null)}catch(e){r(null)}})}
function tx(mode,fn){return new Promise((res,rej)=>{const t=db.transaction('q',mode),q=fn(t.objectStore('q'));
 t.oncomplete=()=>res(q&&q.result);t.onerror=()=>rej(t.error);t.onabort=()=>rej(t.error)})}
async function put(it){
 if(it&&it.meta&&!it.meta.sealed&&!it.carried){
  if(!it.meta.device_id)it.meta.device_id=DEVID;
  const nm=($('me')&&$('me').value||kept('biruni-me')||'').trim().slice(0,40);
  if(!it.meta.by&&nm)it.meta.by=nm;
  if(!it.meta.surveyor)it.meta.surveyor=it.meta.by||nm;
  if(!SKEW_OK){it.tu=1;it.t0=it.meta.t;it.meta.tu=1}}
 if(it&&typeof it.st!=='number')it.st=0;
 if(it&&typeof it.c!=='number')it.c=0;
 if(it&&!it.at)it.at=NOW();
 it=await sealRecord(it);if(db){try{await tx('readwrite',s=>s.put(it));return it}catch(e){
  if(e&&(e.name==='QuotaExceededError'||e.code===22||/quota/i.test(String(e.message||e)))&&typeof setQuotaStopped==='function')setQuotaStopped(true)}}
 mem=mem.filter(x=>x.id!==it.id);mem.push(it);return it}
async function all(){let a=[];if(db){try{a=await tx('readonly',s=>s.getAll())||[]}catch(e){}}
 return a.concat(mem).filter(x=>(x.room||'')===ROOM)}
async function getItem(id){if(db){try{const x=await tx('readonly',s=>s.get(id));if(x)return x}catch(e){}}
 return mem.find(x=>x.id===id)||null}
async function setState(id,st,c,why){
 const x=await getItem(id);if(!x)return null;
 const nc=Math.max(x.c||0,typeof c==='number'?c:0);
 if(st===3||nc>=2){await del(id);mark(id,'✓✓');await count();return {id:id,st:3,c:Math.max(2,nc)}}
 x.st=st;x.c=nc;x.at=NOW();
 if(why!=null)x.why=String(why);
 if(st>=1){x.prev=null;x.prevSent=true}
 if(db){try{await tx('readwrite',s=>s.put(x))}catch(e){}}
 const m=mem.find(y=>y.id===id);if(m)Object.assign(m,x);
 mark(id,badgeFor(x),x.why);await count();return x}
async function sealWaiting(){if(!PCKEY)return;const a=await all();
 for(const x of a)if(x.meta&&!x.meta.sealed)await put(x)}
async function del(id){mem=mem.filter(x=>x.id!==id);if(db){try{await tx('readwrite',s=>s.delete(id))}catch(e){}}}
/* the preview arrived: mark the photo, only if it is still waiting (Google Drive may have taken it meanwhile) */
async function prevDone(id){if(db){try{const x=await tx('readonly',s=>s.get(id));
   if(x){x.prevSent=true;x.prev=null;await tx('readwrite',s=>s.put(x));return true}}catch(e){}}
 const m=mem.find(x=>x.id===id);if(m){m.prevSent=true;m.prev=null;return true}return false}
function setPrivate(on){privMode=!!on;const b=$('privWarn');if(!b)return;
 b.textContent=T.incognito||'Private browsing is on: closing this page will erase waiting photos. Open the link in a normal browser tab.';
 b.classList.toggle('hide',!privMode)}
async function checkPrivate(){if(window.BIRUNI_INCOGNITO!=null){setPrivate(!!window.BIRUNI_INCOGNITO);return privMode}
 if(!db){setPrivate(true);return true}
 try{localStorage.setItem('__bt','1');localStorage.removeItem('__bt')}catch(e){setPrivate(true);return true}
 if(navigator.storage&&navigator.storage.estimate){
  try{const e=await navigator.storage.estimate();if(e&&e.quota&&e.quota<120*1024*1024){setPrivate(true);return true}}
  catch(e){setPrivate(true);return true}}
 setPrivate(false);return false}
function chip(state){$('dot').className='dot '+state;$('chipText').textContent=state==='on'?T.online:state==='wait'?T.linking:T.offline}
function status(on,key){lastSt=[on,key];const b=$('st');chip(on?'on':key==='connecting'?'wait':'off');
 if(on){b.classList.add('hide')}else{
  b.textContent=key==='ladVia'&&typeof LAD==='object'
   ?(T.ladVia||'Sending through: {name}').replace('{name}',LAD.viaName()):(T[key]||key);
  b.classList.remove('hide')}}
let tt=null;function toast(s){const t=$('toast');t.textContent=s;t.classList.add('show');clearTimeout(tt);
 tt=setTimeout(()=>t.classList.remove('show'),2200)}
function mark(id,s,why){const e=document.querySelector('[data-id="'+id+'"] .badge');if(!e)return;e.textContent=s;
 e.className='badge'+((s==='✓'||s==='✓✓')?' ok':s==='✗'?' bad':s==='☁️'?' cloud':'');
 if(why)e.title=String(why);else e.removeAttribute('title')}
async function count(){const a=(await all()).filter(x=>!x.cap),
  a0=a.filter(x=>itemSt(x)===0),a1=a.filter(x=>itemSt(x)===1),a2=a.filter(x=>itemSt(x)===2),a9=a.filter(x=>itemSt(x)===9),
  n0=a0.length,n1=a1.length,n2=a2.length,n9=a9.length,n=n0+n1+n2,p=a0.filter(x=>x.prevSent).length,c=a.filter(x=>x.carried&&itemSt(x)<3).length;
 $('nSent').textContent=sent;$('nWait').textContent=n;
 const wTxt=(T.waitingPhotos||'{n} photos waiting').replace('{n}',n0-p),
  pTxt=(T.previewSent||'{n} arrived as preview, original waiting for Wi-Fi').replace('{n}',p),
  dTxt=(T.drivePreview||'').replace('{n}',p),
  wt0=p?(p===n0?(typeof DRV==='object'&&DRV.on&&DRV.go()&&dTxt?dTxt:pTxt):wTxt+' · '+pTxt):(T.waitingPhotos||'{n} photos waiting').replace('{n}',n0);
 let wt=wt0;
 if(n1||n2||n9){const parts=[];
  if(n0)parts.push(wt0);
  if(n1)parts.push((T.countCloud||'☁️ {n} in the cloud, waiting for the office').replace('{n}',n1));
  if(n2)parts.push((T.countOffice||'{n} at the office, waiting for 2nd copy').replace('{n}',n2));
  if(n9)parts.push((T.countRefused||'{n} refused — kept on this phone').replace('{n}',n9));
  wt=parts.join(' · ')}
 const hasPending=(n+n9)>0;
 if($('waitPill')){if(hasPending){$('waitPill').textContent=wt;$('waitPill').classList.remove('hide')}
  else if(typeof LAD==='object'&&LAD._items.length)LAD.drawPill();
  else $('waitPill').classList.add('hide')}
 $('lWait').textContent=hasPending?wt:bare(T.queued);
 $('handInfo').textContent=c?T.handCarry.replace('{n}',c):'';
 $('waitBox').classList.toggle('warn',hasPending);
 if(typeof updateUnsentBanner==='function')await updateUnsentBanner();
 if(typeof checkStorageSpace==='function')checkStorageSpace()}
/*SEND{*/
/* the phone talks to the PC's own server: one request per photo */
/* every request has a time limit: a photo whose sending stalled (mobile data, the tunnel) waited for ever, the
   phone never tried again and said "Waiting to send" while the computer got nothing */
function within(ms){const c=new AbortController();setTimeout(()=>c.abort(),ms);return c.signal}
async function sendItem(it){const m=Object.assign({},it.meta);
 if((it.tu||(it.meta&&it.meta.tu))&&SKEW_OK)m.dt=Math.max(-2592000000,Math.min(2592000000,SKEW));
 const r=await fetch('photo?m='+encodeURIComponent(JSON.stringify(m)),{method:'POST',
  body:it.blob,headers:{'Content-Type':it.blob.type||'image/jpeg'},signal:within(60000+it.blob.size/20)});
 let j=null;try{j=await r.json()}catch(e){}
 if(j&&typeof j.rt==='number')noteTime(j.rt);
 if(r.status===200&&j&&typeof j.c==='number'){setNl(true);return {status:200,c:j.c,h:j.h||'',rt:j.rt||0,nl:true}}
 if(r.status!==200&&j&&j.reason)return {status:r.status,reason:j.reason};
 return r.status}
async function alive(){try{const r=await fetch('ping',{cache:'no-store',signal:within(15000)});if(!r.ok)return false;
 try{const j=await r.json();if(j&&j.nl===1)setNl(true);if(j&&typeof j.now==='number')noteTime(j.now);
  if(j&&typeof j.eod==='number')setEodHour(j.eod)}catch(e){}
 return true}catch(e){return false}}
async function copyPoll(){const a=(await all()).filter(x=>{const s=itemSt(x);return s===1||s===2}).slice(0,100);
 if(!a.length)return;
 try{const r=await fetch('copy-status',{method:'POST',body:JSON.stringify({ids:a.map(x=>x.id)}),
  headers:{'Content-Type':'application/json'},signal:within(15000)});
  if(!r.ok)return;const j=await r.json();if(j&&typeof j.rt==='number')noteTime(j.rt);
  for(const row of (j&&j.items)||[]){if(!Array.isArray(row)||row.length<2)continue;
   const [id,c,h8]=row,it=await getItem(id);if(!it)continue;
   if(it.h&&h8&&!it.h.startsWith(h8))continue;
   if(c>=2)await setState(id,3,c);else if(c===1&&itemSt(it)<2)await setState(id,2,1)}}catch(e){}}
function offline(){status(false,'lost')}
async function sendPos(p){const t0=performance.now();
 await fetch('pos',{method:'POST',body:JSON.stringify(p),headers:{'Content-Type':'application/json'},signal:within(15000)});
 PING=Math.round(performance.now()-t0)}
async function pollGuide(){try{const g=await (await fetch('guide?v='+GUIDE.v,{cache:'no-store',signal:within(15000)})).json();
 if(g&&g.v!==GUIDE.v)setGuide(g)}catch(e){}}
setInterval(()=>pollGuide(),5000);
/*}SEND*/
/*HAND{*/
/* ---- without internet: hand the waiting photos to a teammate's phone --------------------------------------------
   Two phones on one Wi-Fi or hotspot (no internet needed) link directly after showing each other a QR code each.
   Every photo is sealed for the computer (ECDH P-256 + AES-GCM to its key): the teammate carries it and cannot open
   it; whichever phone reaches the computer first delivers it, and the computer keeps one copy. */
const H64={enc:b=>{let s='';const a=new Uint8Array(b);for(let i=0;i<a.length;i++)s+=String.fromCharCode(a[i]);
  return btoa(s).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')},
 dec:s=>{s=String(s||'');return Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')+'==='.slice((s.length+3)%4)),c=>c.charCodeAt(0))}};
const hcat=(...a)=>{const o=new Uint8Array(a.reduce((n,x)=>n+x.length,0));let i=0;for(const x of a){o.set(x,i);i+=x.length}return o};
const sleepH=ms=>new Promise(r=>setTimeout(r,ms));
async function sealFor(pub,bytes){const pk=await crypto.subtle.importKey('raw',H64.dec(pub),{name:'ECDH',namedCurve:'P-256'},false,[]);
 const eph=await crypto.subtle.generateKey({name:'ECDH',namedCurve:'P-256'},true,['deriveBits']);
 const bits=await crypto.subtle.deriveBits({name:'ECDH',public:pk},eph.privateKey,256);
 const key=await crypto.subtle.importKey('raw',await crypto.subtle.digest('SHA-256',bits),'AES-GCM',false,['encrypt']);
 const iv=crypto.getRandomValues(new Uint8Array(12)),raw=new Uint8Array(await crypto.subtle.exportKey('raw',eph.publicKey));
 return hcat(new Uint8Array([1]),raw,iv,new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:iv},key,bytes)))}
/* ---- no-loss P3: the signed recipients list and envelope v2 (field_photos/recipients.py, envelope.py) ----
   The PC signs the list with the identity behind the link's &i= pin (links without a pin: the first signer seen).
   With a list, each item is sealed once for every computer whose scope allows it (the office, a backup PC, the
   emergency key); without one (an older PC) v1 for the office alone, as before. Items already sealed stay as they are. */
const RLS={room:null,v:null},RL_ROLES=['office','backup','emergency','sup'],
 RL_DEF={office:['photo','state','pos'],backup:['photo','state','pos'],emergency:['photo','state','pos'],sup:['pos']};
const hhex=b=>[...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('');
const kid8=async p=>new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode('biruni-drive-kid-1:'+p))).slice(0,8);
function rlGet(){if(RLS.room!==ROOM){RLS.room=ROOM;RLS.v=null;
  try{const c=JSON.parse(kept('biruni-rl-'+ROOM)||'null');if(c&&c.lst&&Array.isArray(c.lst.r))RLS.v=c}catch(e){}}
 return RLS.v}
function rlBad(){if(typeof SIG==='object'&&SIG)SIG.refused=(SIG.refused||0)+1;return false}
async function rlSigOk(pub,sig,text){try{const raw=H64.dec(pub);if(raw.length!==65)return false;
  const k=await crypto.subtle.importKey('raw',raw,{name:'ECDSA',namedCurve:'P-256'},false,['verify']);
  return await crypto.subtle.verify({name:'ECDSA',hash:'SHA-256'},k,H64.dec(sig),new TextEncoder().encode(text))}catch(e){return false}}
/* pk: the office key the list must name (a hello's new pk); default the key this phone seals for. Links without a
   pin take no list at all (anyone with the bridge key could add themselves): they keep sealing v1. */
async function rlTake(rl,pk){try{
 if(!rl||typeof rl.l!=='string'||typeof rl.ip!=='string'||typeof rl.is!=='string'||rl.l.length>20000)return false;
 const want=pk||PCKEY;if(!ROOM||!want)return false;
 const cur=rlGet();if(cur&&cur.l===rl.l&&!pk)return true;
 const pin=typeof ID==='object'&&ID&&ID.pin?ID.pin:null;if(!pin)return false;
 const text='BRRL1|'+hhex(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(rl.l)));
 if(typeof IDOK!=='function'||!await IDOK(rl.ip,rl.is,text))return rlBad();
 const lst=JSON.parse(rl.l);
 if(!lst||lst.v!==1||typeof lst.seq!=='number'||!Array.isArray(lst.r)||lst.room!==ROOM)return rlBad();
 if(lst.r.length<1||lst.r.length>12)return rlBad();
 const offs=lst.r.filter(e=>e&&e.role==='office');
 if(offs.length!==1||offs[0].p!==want)return rlBad();                /* the office must be the key this phone seals for */
 const curOff=cur?(cur.lst.r.find(e=>e.role==='office')||{}).p:null;
 if(cur&&curOff===want&&lst.seq<cur.lst.seq)return rlBad();           /* a rollback: an older list for the same office */
 const seen=new Set();
 for(const e of lst.r){if(!e||RL_ROLES.indexOf(e.role)<0||typeof e.p!=='string'||!/^[\w-]{87}$/.test(e.p))return rlBad();
  const raw=H64.dec(e.p);if(raw.length!==65||raw[0]!==4)return rlBad();
  const k=hhex(await kid8(e.p));if(seen.has(k))return rlBad();seen.add(k)}
 const v={l:rl.l,lst:lst};keep('biruni-rl-'+ROOM,JSON.stringify(v));RLS.room=ROOM;RLS.v=v;return true}catch(e){return rlBad()}}
/* a hello or guide brings a pk: with a list kept, only a signed list naming that key may change it */
async function rlPk(pk,rl){const cur=rlGet();
 const curOff=cur?(cur.lst.r.find(e=>e.role==='office')||{}).p:null;
 if(!curOff||curOff===pk){if(rl)await rlTake(rl,pk);return true}
 return !!(rl&&await rlTake(rl,pk))}
/* the keys one kind of item ('photo', 'state', 'pos') is sealed for, office first; null: v1 */
function rlFor(kind){const v=rlGet();if(!v||!PCKEY)return null;
 const off=v.lst.r.find(e=>e.role==='office');if(!off||off.p!==PCKEY)return null;   /* a list for another key: v1 */
 const ps=[PCKEY];
 for(const e of v.lst.r){if(e.role==='office')continue;
  const sc=Array.isArray(e.scope)?e.scope:(RL_DEF[e.role]||[]);if(sc.indexOf(kind)>=0&&ps.indexOf(e.p)<0)ps.push(e.p)}
 return ps.length>1?ps:null}
/* envelope v2: 0x02 | n | eph(65) | n x [kid(8) | wrap(48)] | iv(12) | ct  (envelope.py; fx: the tests' fixed inputs) */
async function sealFor2(pubs,bytes,fx){
 const eph=fx&&fx.eph||await crypto.subtle.generateKey({name:'ECDH',namedCurve:'P-256'},true,['deriveBits']);
 const er=new Uint8Array(await crypto.subtle.exportKey('raw',eph.publicKey));
 const ck=fx&&fx.ck||crypto.getRandomValues(new Uint8Array(32)),iv=fx&&fx.iv||crypto.getRandomValues(new Uint8Array(12));
 const parts=[new Uint8Array([2,pubs.length]),er],info=new TextEncoder().encode('biruni-seal-2');
 for(const p of pubs){const rr=H64.dec(p);
  const pk=await crypto.subtle.importKey('raw',rr,{name:'ECDH',namedCurve:'P-256'},false,[]);
  const bits=await crypto.subtle.deriveBits({name:'ECDH',public:pk},eph.privateKey,256);
  const hk=await crypto.subtle.importKey('raw',bits,'HKDF',false,['deriveKey']);
  const kek=await crypto.subtle.deriveKey({name:'HKDF',hash:'SHA-256',salt:hcat(er,rr),info:info},hk,
   {name:'AES-GCM',length:256},false,['encrypt']);
  parts.push(await kid8(p),new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:new Uint8Array(12)},kek,ck)))}
 const head=hcat(...parts),k=await crypto.subtle.importKey('raw',ck,'AES-GCM',false,['encrypt']);
 return hcat(head,iv,new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:iv,additionalData:head},k,bytes)))}
async function sealItem(it){if(it.meta&&it.meta.sealed)return it.blob;   /* already sealed for the PC: kept as it is */
 const m=new TextEncoder().encode(JSON.stringify(it.meta)),img=new Uint8Array(await it.blob.arrayBuffer()),n=new Uint8Array(4);
 new DataView(n.buffer).setUint32(0,m.length);const ps=rlFor(it.cap?'state':'photo');
 return new Blob([ps?await sealFor2(ps,hcat(n,m,img)):await sealFor(PCKEY,hcat(n,m,img))],{type:'application/octet-stream'})}
/* the link's offer and answer, short enough for a QR code: only what a direct link on one network needs */
function sdpCode(sdp,kind){const g=r=>(sdp.match(r)||[])[1]||'',fp=g(/a=fingerprint:sha-256 ([0-9A-Fa-f:]+)/);
 const c=[...sdp.matchAll(/a=candidate:\S+ 1 udp \d+ (\S+) (\d+) typ host/gi)].map(x=>x[1]+'_'+x[2]).filter(x=>!x.includes(':')).slice(0,4);
 return 'B1'+kind+'|'+g(/a=ice-ufrag:(\S+)/)+'|'+g(/a=ice-pwd:(\S+)/)+'|'+H64.enc(Uint8Array.from(fp.split(':'),h=>parseInt(h,16)))+'|'+c.join(',')}
function codeSdp(code){const p=String(code||'').split('|');if(p.length!==5||!/^B1[oa]$/.test(p[0])||!p[1]||!p[2])return null;
 let fp;try{fp=[...H64.dec(p[3])].map(b=>b.toString(16).padStart(2,'0').toUpperCase()).join(':')}catch(e){return null}
 const offer=p[0]==='B1o';
 let s='v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=group:BUNDLE 0\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n'+
  'c=IN IP4 0.0.0.0\r\na=ice-ufrag:'+p[1]+'\r\na=ice-pwd:'+p[2]+'\r\na=fingerprint:sha-256 '+fp+'\r\na=setup:'+(offer?'actpass':'active')+
  '\r\na=mid:0\r\na=sctp-port:5000\r\na=max-message-size:262144\r\n';
 (p[4]?p[4].split(','):[]).forEach((c,i)=>{const j=c.lastIndexOf('_');if(j>0)s+='a=candidate:'+(i+1)+' 1 udp '+(2122260223-i)+' '+
  c.slice(0,j)+' '+c.slice(j+1)+' typ host generation 0\r\n'});
 return {type:offer?'offer':'answer',sdp:s+'a=end-of-candidates\r\n'}}
/*QR{*/
/* QR codes drawn on the phone itself (byte mode, error correction M, versions 1-20): no library, no internet */
const QRM=(()=>{
 const ECC=[0,10,16,26,18,24,16,18,22,22,26,30,22,22,24,24,28,28,26,26,26],BLK=[0,1,1,1,2,2,4,4,4,5,5,5,8,9,9,10,10,11,13,14,16];
 const raw=v=>{let r=(16*v+128)*v+64;if(v>=2){const n=Math.floor(v/7)+2;r-=(25*n-10)*n-55;if(v>=7)r-=36}return r};
 const dataCw=v=>Math.floor(raw(v)/8)-ECC[v]*BLK[v];
 const mul=(x,y)=>{let z=0;for(let i=7;i>=0;i--){z=((z<<1)^((z>>>7)*0x11D))&255;z^=((y>>>i)&1)*x}return z};
 function rsDiv(d){const r=new Array(d).fill(0);r[d-1]=1;let root=1;
  for(let i=0;i<d;i++){for(let j=0;j<d;j++){r[j]=mul(r[j],root);if(j+1<d)r[j]^=r[j+1]}root=mul(root,2)}return r}
 function rsRem(data,div){const r=new Array(div.length).fill(0);
  for(const b of data){const f=b^r.shift();r.push(0);div.forEach((c,i)=>{r[i]^=mul(c,f)})}return r}
 function align(v){if(v===1)return [];const n=Math.floor(v/7)+2,step=Math.ceil((v*4+4)/(n*2-2))*2,out=[6];
  for(let p=v*4+10;out.length<n;p-=step)out.splice(1,0,p);return out}
 const MASK=[(x,y)=>(x+y)%2===0,(x,y)=>y%2===0,(x,y)=>x%3===0,(x,y)=>(x+y)%3===0,(x,y)=>(Math.floor(x/3)+Math.floor(y/2))%2===0,
  (x,y)=>x*y%2+x*y%3===0,(x,y)=>(x*y%2+x*y%3)%2===0,(x,y)=>((x+y)%2+x*y%3)%2===0];
 function penalty(m,n){let p=0;
  for(let a=0;a<2;a++)for(let i=0;i<n;i++){let run=1;for(let j=1;j<=n;j++){
   const same=j<n&&(a?m[j][i]===m[j-1][i]:m[i][j]===m[i][j-1]);if(same)run++;else{if(run>=5)p+=run-2;run=1}}}
  for(let y=0;y<n-1;y++)for(let x=0;x<n-1;x++){const c=m[y][x];if(c===m[y][x+1]&&c===m[y+1][x]&&c===m[y+1][x+1])p+=3}
  let dark=0;m.forEach(r=>r.forEach(c=>{dark+=c?1:0}));return p+Math.max(0,Math.ceil(Math.abs(dark*20-n*n*10)/(n*n))-1)*10}
 return (text,force)=>{const bytes=[...new TextEncoder().encode(text)];let v=1;
  for(;v<=20;v++)if(4+(v<10?8:16)+bytes.length*8<=dataCw(v)*8)break;
  if(v>20)throw new Error('too long for a code');
  const bits=[],put=(val,n)=>{for(let i=n-1;i>=0;i--)bits.push((val>>>i)&1)};
  put(4,4);put(bytes.length,v<10?8:16);bytes.forEach(b=>put(b,8));
  const cap=dataCw(v)*8;put(0,Math.min(4,cap-bits.length));put(0,(8-bits.length%8)%8);
  for(let p=0xEC;bits.length<cap;p^=0xEC^0x11)put(p,8);
  const data=[];for(let i=0;i<bits.length;i+=8){let b=0;for(let j=0;j<8;j++)b=(b<<1)|bits[i+j];data.push(b)}
  const nb=BLK[v],ec=ECC[v],rawCw=Math.floor(raw(v)/8),nShort=nb-rawCw%nb,shortLen=Math.floor(rawCw/nb),div=rsDiv(ec),blocks=[];
  for(let i=0,k=0;i<nb;i++){const dat=data.slice(k,k+shortLen-ec+(i<nShort?0:1));k+=dat.length;const e=rsRem(dat,div);
   if(i<nShort)dat.push(0);blocks.push(dat.concat(e))}
  const cw=[];for(let i=0;i<blocks[0].length;i++)blocks.forEach((b,j)=>{if(i!==shortLen-ec||j>=nShort)cw.push(b[i])});
  const n=v*4+17,m=[...Array(n)].map(()=>new Array(n).fill(false)),fn=[...Array(n)].map(()=>new Array(n).fill(false));
  const set=(x,y,d)=>{m[y][x]=d;fn[y][x]=true};
  for(let i=0;i<n;i++){set(6,i,i%2===0);set(i,6,i%2===0)}
  const finder=(x,y)=>{for(let dy=-4;dy<=4;dy++)for(let dx=-4;dx<=4;dx++){const d=Math.max(Math.abs(dx),Math.abs(dy)),xx=x+dx,yy=y+dy;
   if(xx>=0&&xx<n&&yy>=0&&yy<n)set(xx,yy,d!==2&&d!==4)}};
  finder(3,3);finder(n-4,3);finder(3,n-4);
  const al=align(v),last=al.length-1;
  al.forEach((a,i)=>al.forEach((b,j)=>{if((i===0&&j===0)||(i===0&&j===last)||(i===last&&j===0))return;
   for(let dy=-2;dy<=2;dy++)for(let dx=-2;dx<=2;dx++)set(a+dx,b+dy,Math.max(Math.abs(dx),Math.abs(dy))!==1)}));
  const format=mask=>{const d=mask;let r=d;for(let i=0;i<10;i++)r=(r<<1)^((r>>>9)*0x537);
   const f=((d<<10)|r)^0x5412,g=i=>((f>>>i)&1)===1;
   for(let i=0;i<=5;i++)set(8,i,g(i));set(8,7,g(6));set(8,8,g(7));set(7,8,g(8));for(let i=9;i<15;i++)set(14-i,8,g(i));
   for(let i=0;i<8;i++)set(n-1-i,8,g(i));for(let i=8;i<15;i++)set(8,n-15+i,g(i));set(8,n-8,true)};
  format(0);
  if(v>=7){let r=v;for(let i=0;i<12;i++)r=(r<<1)^((r>>>11)*0x1F25);const f=(v<<12)|r;
   for(let i=0;i<18;i++){const d=((f>>>i)&1)===1,a=n-11+i%3,c=Math.floor(i/3);set(a,c,d);set(c,a,d)}}
  let i=0;for(let right=n-1;right>=1;right-=2){if(right===6)right=5;
   for(let vert=0;vert<n;vert++)for(let j=0;j<2;j++){const x=right-j,y=((right+1)&2)===0?n-1-vert:vert;
    if(!fn[y][x]&&i<cw.length*8){m[y][x]=((cw[i>>>3]>>>(7-(i&7)))&1)===1;i++}}}
  const apply=k=>{for(let y=0;y<n;y++)for(let x=0;x<n;x++)if(!fn[y][x]&&MASK[k](x,y))m[y][x]=!m[y][x]};
  let best=force;
  if(best==null){let low=Infinity;for(let k=0;k<8;k++){apply(k);format(k);const p=penalty(m,n);if(p<low){low=p;best=k}apply(k)}}
  apply(best);format(best);return m}})();
/*}QR*/
function drawQr(c,text){const m=QRM(text),n=m.length,q=4,s=Math.max(2,Math.floor(640/(n+2*q)));
 c.width=c.height=(n+2*q)*s;const g=c.getContext('2d');g.fillStyle='#fff';g.fillRect(0,0,c.width,c.height);g.fillStyle='#000';
 m.forEach((r,y)=>r.forEach((d,x)=>{if(d)g.fillRect((x+q)*s,(y+q)*s,s,s)}))}
let HAND=null;   /* {role,pc,ch,stream,timer,onRead,deadline,done,n,got} */
function handText(s){$('handStep').textContent=s}
function handOpen(){$('handSheet').classList.remove('hide');$('handQr').classList.add('hide');$('handScan').classList.add('hide')}
async function handCamera(){if(window.HAND_TEST)return true;
 if(!('BarcodeDetector' in window)||!navigator.mediaDevices)return false;
 /* the camera also lets the link use the phone's own address on the hotspot (not a hidden .local name) */
 try{HAND.stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}});return true}catch(e){return false}}
async function handScan(onRead){const h=HAND;h.onRead=onRead;if(window.HAND_TEST||!h.stream)return;
 $('handScan').classList.remove('hide');const v=$('handVideo');v.srcObject=h.stream;try{await v.play()}catch(e){}
 const det=new BarcodeDetector({formats:['qr_code']});clearInterval(h.timer);
 h.timer=setInterval(async()=>{try{const c=await det.detect(v);if(c.length)handRead(c[0].rawValue)}catch(e){}},300)}
async function handRead(text){const h=HAND;if(!h||!h.onRead||h.reading)return false;h.reading=true;let ok=false;
 try{ok=await h.onRead(String(text||''))}catch(e){}h.reading=false;
 if(ok){h.onRead=null;clearInterval(h.timer);$('handScan').classList.add('hide')}
 else if(!h.warned){h.warned=true;toast(T.handBad)}return ok}
function handShow(code){window._handCode=code;const c=$('handQr');drawQr(c,code);c.classList.remove('hide')}
function handClose(){const h=HAND;HAND=null;if(!h)return;clearInterval(h.timer);clearTimeout(h.deadline);
 if(h.stream)h.stream.getTracks().forEach(t=>t.stop());try{if(h.pc)h.pc.close()}catch(e){}$('handSheet').classList.add('hide')}
function handFail(){if(HAND&&!HAND.done){HAND.done=true;handClose();toast(T.handFail)}}
function handGathered(pc){return new Promise(r=>{if(pc.iceGatheringState==='complete')return r();const t=setTimeout(r,2500);
 pc.addEventListener('icegatheringstatechange',()=>{if(pc.iceGatheringState==='complete'){clearTimeout(t);r()}})})}
async function handGive(){if(HAND)return;const items=await all();
 if(!items.length){toast(T.handNone);return}
 if(items.some(it=>!(it.meta&&it.meta.sealed))&&!PCKEY){toast(T.handNoKey);return}
 const h=HAND={role:'give',got:new Set(),n:0};handOpen();handText(T.handWait);
 if(!await handCamera()){handClose();toast(T.handNoScan);return}
 if(HAND!==h)return;h.pc=new RTCPeerConnection({iceServers:[]});
 const ch=h.ch=h.pc.createDataChannel('hand',{ordered:true});ch.binaryType='arraybuffer';
 ch.onopen=()=>{$('handQr').classList.add('hide');handSend(h,ch)};
 ch.onmessage=e=>{if(typeof e.data!=='string')return;let m;try{m=JSON.parse(e.data)}catch(x){return}if(m.t==='got')h.got.add(m.id)};
 ch.onclose=()=>{if(HAND===h&&!h.done)handFail()};
 await h.pc.setLocalDescription(await h.pc.createOffer());await handGathered(h.pc);if(HAND!==h)return;
 handShow(sdpCode(h.pc.localDescription.sdp,'o'));handText(T.handGive1);h.deadline=setTimeout(handFail,180000);
 handScan(async text=>{const d=codeSdp(text);if(!d||d.type!=='answer')return false;
  await h.pc.setRemoteDescription(d);handText(T.handLinking);return true})}
async function handSend(h,ch){const items=(await all()).filter(it=>it.meta&&(it.meta.sealed||PCKEY));
 const say=()=>handText(T.handSending.replace('{n}',h.n).replace('{k}',items.length));say();
 try{for(const it of items){const buf=new Uint8Array(await (await sealItem(it)).arrayBuffer());
   ch.send(JSON.stringify({t:'item',id:it.id,at:it.meta.t||Date.now(),size:buf.length}));
   for(let o=0;o<buf.length;o+=16384){while(ch.bufferedAmount>1048576){if(ch.readyState!=='open')throw new Error('closed');
     await sleepH(25)}ch.send(buf.slice(o,o+16384))}
   ch.send(JSON.stringify({t:'end',id:it.id}));const t0=Date.now();
   while(!h.got.has(it.id)){if(ch.readyState!=='open'||Date.now()-t0>60000)throw new Error('lost');await sleepH(30)}
   h.n++;say()}
  ch.send(JSON.stringify({t:'done'}));h.done=true;await sleepH(400);handClose();toast(T.handGave.replace('{n}',h.n))}
 catch(e){h.done=true;handClose();toast(h.n?T.handGave.replace('{n}',h.n):T.handFail)}}
async function handTake(){if(HAND)return;const h=HAND={role:'take',n:0};handOpen();handText(T.handWait);
 if(!await handCamera()){handClose();toast(T.handNoScan);return}
 if(HAND!==h)return;handText(T.handTake1);
 handScan(async text=>{const d=codeSdp(text);if(!d||d.type!=='offer')return false;
  h.pc=new RTCPeerConnection({iceServers:[]});h.pc.ondatachannel=e=>handReceive(h,e.channel);
  await h.pc.setRemoteDescription(d);await h.pc.setLocalDescription(await h.pc.createAnswer());await handGathered(h.pc);
  if(HAND!==h)return true;handShow(sdpCode(h.pc.localDescription.sdp,'a'));handText(T.handTake2);
  h.deadline=setTimeout(handFail,180000);return true})}
function handReceive(h,ch){ch.binaryType='arraybuffer';let cur=null,q=Promise.resolve();
 const said=()=>handText(T.handTaking.replace('{n}',h.n));
 ch.onopen=()=>{$('handQr').classList.add('hide');said()};
 ch.onmessage=e=>{const d=e.data;q=q.then(async()=>{
  if(typeof d!=='string'){if(cur){cur.parts.push(d);cur.got+=d.byteLength;if(cur.got>cur.size)cur=null}return}
  let m;try{m=JSON.parse(d)}catch(x){return}
  if(m.t==='item'&&m.id&&+m.size>0&&+m.size<=41943040)cur={id:String(m.id).replace(/[^A-Za-z0-9-]/g,'').slice(0,80),
   at:+m.at||Date.now(),size:+m.size,parts:[],got:0};
  else if(m.t==='end'&&cur&&m.id&&cur.id===String(m.id).replace(/[^A-Za-z0-9-]/g,'').slice(0,80)){const c=cur;cur=null;
   if(c.got!==c.size)return;
   if(!(await all()).some(x=>x.id===c.id))await put({id:c.id,blob:new Blob(c.parts,{type:'application/octet-stream'}),
    meta:{id:c.id,sealed:1,t:c.at},room:ROOM,carried:true});
   h.n++;said();ch.send(JSON.stringify({t:'got',id:m.id}))}
  else if(m.t==='done'){h.done=true;await sleepH(300);handClose();toast(T.handTook.replace('{n}',h.n));count();flush()}}).catch(()=>{})};
 ch.onclose=()=>{if(HAND===h&&!h.done){h.done=true;handClose();toast(h.n?T.handTook.replace('{n}',h.n):T.handFail);count()}}}
/*}HAND*/
/*CAP{*/
var CAP=null,CAPJOB='',CAPSEL=null,CAPSHEET=null,CAPTM=0,CAPLASTFIX=null,CAPST=null;
function capRand(n){const a='abcdefghijklmnopqrstuvwxyz0123456789',r=crypto.getRandomValues(new Uint8Array(n));
 let s='';for(let i=0;i<n;i++)s+=a[r[i]%36];return s}
function capEl(tag,cls,txt){const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e}
function capJobObj(){return CAP&&Array.isArray(CAP.jobs)&&CAP.jobs.find(j=>j&&j.id===CAPJOB&&j.st!=='closed')||null}
function capSchema(){const j=capJobObj();return j&&CAP&&CAP.schemas&&CAP.schemas[j.s]||null}
function capStateKey(jid){return 'biruni-job-'+ROOM+'-'+jid}
function capWho(){return (SURVNAME||$('me').value||kept('biruni-me')||'').trim().slice(0,40)}
function capLoadState(jid,sref){if(!jid)return null;
 try{const st=JSON.parse(kept(capStateKey(jid))||'null');
  if(st&&st.job===jid&&typeof st.inst==='object')return st}catch(e){}
 return {v:1,job:jid,dev:DEVID,by:capWho(),rev:0,t:Date.now(),s:sref||'',inst:{},ans:{},note:{},ph:{},skip:{},closed:null}}
const capSaveLocal=()=>{if(CAPST&&CAPJOB)keep(capStateKey(CAPJOB),JSON.stringify(CAPST))};
function capTouch(){if(!CAPST)return;CAPST.rev=(+CAPST.rev||0)+1;CAPST.t=Date.now();CAPST.by=capWho();
 capSaveLocal();clearTimeout(CAPTM);CAPTM=setTimeout(capSnap,3000)}
async function capSnap(){clearTimeout(CAPTM);CAPTM=0;if(!CAPST||!CAPJOB)return null;
 const jid=CAPJOB,rev=+CAPST.rev||1,now=Date.now(),id='js'+rev.toString(36)+'-'+capRand(9);
 const snap=Object.assign({},CAPST,{dev:DEVID,rev:rev,t:CAPST.t||now});
 const blob=new Blob(['BCS1'+JSON.stringify(snap)],{type:'application/octet-stream'});
 const meta={id:id,t:now,cs:{j:jid,rev:rev},device_id:DEVID,surveyor:snap.by||'',by:snap.by||'',sk:SURVKEY||undefined};
 for(const x of await all()){if(x.cap&&(x.csj===jid||(x.meta&&x.meta.cs&&x.meta.cs.j===jid))){
  if((typeof DRV==='object'&&DRV.holds&&DRV.holds(x.id))||(typeof acks==='object'&&acks&&acks.has(x.id)))continue;
  await del(x.id)}}
 const it=await put({id:id,blob:blob,meta:meta,room:ROOM,cap:1,csj:jid});
 flush();if(typeof drivePump==='function')drivePump();return it}
function capFallbackFix(){const j=capJobObj();if(!j)return null;
 if(CAPLASTFIX&&CAPLASTFIX.j===j.id)return {lat:CAPLASTFIX.lat,lon:CAPLASTFIX.lon,acc:CAPLASTFIX.acc||999,alt:null,heading:null,t:Date.now()};
 const l=j.loc;return Array.isArray(l)&&l.length===2&&isFinite(l[0])&&isFinite(l[1])?{lat:+l[1],lon:+l[0],acc:999,alt:null,heading:null,t:Date.now()}:null}
function capSyncShutter(){const sf=shotFix(),blk=(typeof iosBlocked!=='undefined'&&iosBlocked)||(typeof quotaStopped!=='undefined'&&quotaStopped);
 $('shot').disabled=!sf||blk;$('shotHint').textContent=quotaStopped?(T.quotaStop||''):sf?T.shoot:T.waiting}
function capCleanVal(q,val){if(val==null||val===''||(Array.isArray(val)&&!val.length))return undefined;
 const t=q.type;
 if(t==='text'){const s=String(val).trim().slice(0,250);return s||undefined}
 if(t==='number'||t==='integer'){if(typeof val==='boolean')return undefined;
  const raw=typeof val==='number'?String(val):digits(val),n=Number(raw);
  if(!raw||!isFinite(n)||(t==='integer'&&!Number.isInteger(n))||(q.min!=null&&n<q.min)||(q.max!=null&&n>q.max))return undefined;
  return t==='integer'?Math.trunc(n):n}
 if(t==='choice'){const s=String(val).trim().slice(0,60);return s||undefined}
 if(t==='multi'){const s=(Array.isArray(val)?val:[val]).map(x=>String(x??'').trim().slice(0,60)).filter(Boolean).join('; ').slice(0,500);
  return s||undefined}
 if(t==='yesno')return typeof val==='boolean'?val:undefined;
 if(t==='date'){const s=String(val).trim().slice(0,10);if(!/^\d{4}-\d{2}-\d{2}$/.test(s))return undefined;
  const [y,m,d]=s.split('-').map(Number),dt=new Date(Date.UTC(y,m-1,d));
  return dt.getUTCFullYear()===y&&dt.getUTCMonth()===m-1&&dt.getUTCDate()===d?s:undefined}
 return val}
function capQAnswered(q,qv){if(!qv||!(q.name in qv))return false;const r=qv[q.name];return capCleanVal(q,Array.isArray(r)?r[0]:r)!==undefined}
function capInstTitle(node,k,ansMap,order){const qv=(ansMap&&ansMap[k])||{};
 for(const q of node.q||[])if(q.title){const r=qv[q.name],v=Array.isArray(r)?r[0]:r;if(v!=null&&v!=='')return String(v)}
 return node.label+' '+order}
function capTree(schema,state,records){const s=schema||{nodes:[]},st=state||{},jobId=st.job||'',inst={},rawInst=st.inst||{};
 for(const iid of Object.keys(rawInst))inst[iid]=Object.assign({},rawInst[iid]);
 for(const r of records||[]){const c=r&&r.cap;if(!c||!c.j||(jobId&&c.j!==jobId))continue;
  let p='';for(const e of c.ch||[])if(Array.isArray(e)&&e.length>=3){const [nid,iid,ord]=e;if(!inst[iid])inst[iid]={n:nid,p:p,o:ord,t:0,del:0};p=iid}}
 const ansMap=st.ans||{},noteMap=st.note||{},phMap=st.ph||{},skipMap=st.skip||{},recBySlot={},extraRecs=[];
 for(const r of records||[]){if(!r||!r.id||!r.cap||!r.cap.j||!r.cap.k||(jobId&&r.cap.j!==jobId))continue;
  if(r.cap.k==='*')extraRecs.push(r);else(recBySlot[r.cap.k]||(recBySlot[r.cap.k]=[])).push(r)}
 const skipOf=(k,inh)=>{if(inh)return inh;const p=skipMap[k],txt=String((Array.isArray(p)?p[0]:p)||'').trim();return txt.length>=3?txt:''};
 function evalSlot(node,iid,pathParts,chain,inhSkip,order){
  const nid=node.id,k=iid+':'+nid,req=node.req!==false,ownSkip=skipOf(k,''),skipReason=inhSkip||ownSkip,skipped=!!skipReason;
  const ph=node.photos||[0,0],pmin=ph[0]||0,pmax=ph[1]||0,sRecs=recBySlot[k]||[];
  const allPids=[];for(const pid of sRecs.map(r=>r.id).concat(Array.isArray(phMap[k])?phMap[k]:[]))if(pid&&!allPids.includes(pid))allPids.push(pid);
  const qDefs=node.q||[],qVals=ansMap[k]||{},ansOut={},missingQ=[];
  for(const q of qDefs){if(capQAnswered(q,qVals)){const rv=qVals[q.name];ansOut[q.name]=Array.isArray(rv)?rv[0]:rv}else if(q.req)missingQ.push(q.name)}
  const np=noteMap[k],noteTxt=String((Array.isArray(np)?np[0]:np)||'');
  const children=(node.nodes||[]).map(ch=>evalNode(ch,iid,pathParts,chain,skipReason));
  const ownOk=allPids.length>=pmin&&!missingQ.length,chOk=children.every(c=>c.done||c.skipped||!c.req);
  return {kind:order!=null?'instance':'node',id:nid,iid:iid,order:order!=null?order:null,k:k,
   label:pathParts[pathParts.length-1],path:pathParts.slice(),path_label:pathParts.join(' › '),
   ch:chain.slice(),req:req,photos:[pmin,pmax],photo_count:allPids.length,photo_ids:allPids,
   q:qDefs,answers:ansOut,missing_q:missingQ,note:noteTxt,own_skip:ownSkip,skip:skipReason,
   skipped:skipped,own_done:ownOk,done:ownOk&&chOk,children:children,node:node}}
 function evalNode(node,pIid,pPath,pChain,inhSkip){
  const nid=node.id,rep=node.repeat;if(!rep)return evalSlot(node,pIid,pPath.concat([node.label]),pChain,inhSkip,null);
  const k=pIid+':'+nid,req=node.req!==false,ownSkip=skipOf(k,''),skipReason=inhSkip||ownSkip,skipped=!!skipReason,rmin=rep.min||0,rmax=rep.max||1;
  const live=Object.keys(inst).filter(iid=>{const inf=inst[iid];return inf&&inf.n===nid&&(inf.p||'')===pIid&&!inf.del})
   .map(iid=>[iid,inst[iid]]).sort((a,b)=>((a[1].o||1)-(b[1].o||1))||(a[0]<b[0]?-1:a[0]>b[0]?1:0));
  const instances=live.map(([iid,inf],idx)=>{const ord=inf.o||(idx+1),ttl=capInstTitle(node,iid+':'+nid,ansMap,ord);
   return evalSlot(node,iid,pPath.concat([ttl]),pChain.concat([[nid,iid,ord]]),skipReason,ord)});
  const countOk=instances.length>=rmin,instOk=instances.every(it=>it.done||it.skipped);
  return {kind:'repeat',id:nid,iid:pIid,k:k,label:node.label,path:pPath.concat([node.label]),
   path_label:pPath.concat([node.label]).join(' › '),ch:pChain.slice(),req:req,
   repeat:{min:rmin,max:rmax},instance_count:instances.length,instances:instances,
   own_skip:ownSkip,skip:skipReason,skipped:skipped,count_ok:countOk,done:countOk&&instOk,node:node}}
 const roots=(s.nodes||[]).map(n=>evalNode(n,'',[],[],'')),extraIds=[];
 for(const pid of extraRecs.map(r=>r.id).concat(Array.isArray(phMap['*'])?phMap['*']:[]))if(pid&&!extraIds.includes(pid))extraIds.push(pid);
 return {schema:s,job:jobId,roots:roots,extra_photo_ids:extraIds}}
function capMissing(schemaOrTree,state,records){
 const t=(schemaOrTree&&Array.isArray(schemaOrTree.roots))?schemaOrTree:capTree(schemaOrTree,state,records),out=[];
 function walkSlot(it){if(!it.req||it.skipped)return;
  const needPh=Math.max(0,it.photos[0]-it.photo_count);
  if(needPh>0||it.missing_q.length)out.push({k:it.k,id:it.id,iid:it.iid,path_label:it.path_label,reason:'slot',need_photos:needPh,missing_q:it.missing_q.slice()});
  for(const ch of it.children||[])walk(ch)}
 function walk(it){if(!it.req||it.skipped)return;
  if(it.kind==='repeat'){const rmin=it.repeat.min;
   if(it.instance_count<rmin)out.push({k:it.k,id:it.id,iid:it.iid,path_label:it.path_label,reason:'repeat_min',have:it.instance_count,min:rmin});
   for(const sub of it.instances)walkSlot(sub)}
  else walkSlot(it)}
 for(const r of t.roots)walk(r);return out}
const capLive=(nid,pIid)=>Object.keys(CAPST.inst).filter(iid=>{const inf=CAPST.inst[iid];return inf&&inf.n===nid&&(inf.p||'')===pIid&&!inf.del});
function capEnsureMin(nodes,pIid){if(!capSchema()||!CAPST)return false;let changed=false;
 for(const n of nodes||[]){if(n.repeat){const live=capLive(n.id,pIid);
   let maxO=live.reduce((m,iid)=>Math.max(m,CAPST.inst[iid].o||0),0);
   while(live.length<(n.repeat.min||0)){const iid=capRand(6);CAPST.inst[iid]={n:n.id,p:pIid,o:++maxO,t:Date.now(),del:0};live.push(iid);changed=true}
   for(const iid of live)if(n.nodes&&capEnsureMin(n.nodes,iid))changed=true}
  else if(n.nodes&&capEnsureMin(n.nodes,pIid))changed=true}
 return changed}
function capSelectJob(jid){CAPJOB=jid||'';CAPSEL=null;CAPSHEET=null;keep('biruni-cap-job-'+ROOM,CAPJOB||null);
 const j=capJobObj(),s=capSchema();
 if(j&&s){CAPST=capLoadState(j.id,j.s);if(capEnsureMin(s.nodes,''))capSaveLocal()}else CAPST=null;
 if($('capMiss'))$('capMiss').classList.add('hide');capSyncShutter();capDraw()}
function capSet(c){CAP=(c&&typeof c==='object'&&Array.isArray(c.jobs)&&c.jobs.some(j=>j&&j.st!=='closed'))?c:null;
 keep('biruni-cap-'+ROOM,CAP?JSON.stringify(CAP):null);
 if(!CAP){CAPJOB='';CAPST=null;CAPSEL=null;CAPSHEET=null;$('capCard').classList.add('hide');$('formCard').classList.remove('hide');capSyncShutter();return}
 const open=CAP.jobs.filter(j=>j&&j.st!=='closed'),saved=kept('biruni-cap-job-'+ROOM);
 capSelectJob((CAPJOB&&open.some(j=>j.id===CAPJOB))?CAPJOB:(saved&&open.some(j=>j.id===saved))?saved:open[0].id)}
function capAdd(node,pIid){if(!CAPST||!node||!node.repeat)return null;
 const live=capLive(node.id,pIid),rmax=node.repeat.max||200;
 if(live.length>=rmax){toast((T.capMaxWarn||'At most {n} allowed').replace('{n}',rmax));return null}
 const maxO=live.reduce((m,iid)=>Math.max(m,CAPST.inst[iid].o||0),0),iid=capRand(6);
 CAPST.inst[iid]={n:node.id,p:pIid,o:maxO+1,t:Date.now(),del:0};
 if(node.nodes)capEnsureMin(node.nodes,iid);capTouch();capDraw();return iid}
function capRemove(node,iid){if(!CAPST||!node||!node.repeat||!CAPST.inst[iid])return false;
 const pIid=CAPST.inst[iid].p||'',rmin=node.repeat.min||0;
 if(capLive(node.id,pIid).length<=rmin){toast((T.capMinWarn||'At least {n} required').replace('{n}',rmin));return false}
 const now=Date.now(),markDel=id=>{if(CAPST.inst[id]&&!CAPST.inst[id].del)CAPST.inst[id].del=now;
  for(const c of Object.keys(CAPST.inst))if((CAPST.inst[c].p||'')===id)markDel(c)};
 markDel(iid);if(CAPSHEET&&CAPSHEET.iid===iid)capCloseSheet();capTouch();capDraw();return true}
function capProgress(t){let done=0,total=0;
 function walkSlot(it){if(!(it.children&&it.children.length)||it.photos[0]>0||it.q.length>0){
   if(it.req||it.own_done||it.skipped){total++;if(it.own_done||it.skipped)done++}}
  for(const ch of it.children||[])walk(ch)}
 function walk(it){if(it.kind==='repeat'){if(it.req&&it.instance_count<it.repeat.min)total+=it.repeat.min-it.instance_count;
   for(const sub of it.instances)walkSlot(sub)}else walkSlot(it)}
 for(const r of t.roots)walk(r);return {done:done,total:total}}
function capDraw(){const card=$('capCard');if(!card)return;
 const open=CAP&&Array.isArray(CAP.jobs)?CAP.jobs.filter(j=>j&&j.st!=='closed'):[];
 if(!open.length){card.classList.add('hide');$('formCard').classList.remove('hide');return}
 card.classList.remove('hide');$('lCap').textContent=T.capJobs||'Capture job';
 const sel=$('capJob');sel.textContent='';
 const opt0=capEl('option','',T.capNoJob||'No job (free photos)');opt0.value='';sel.appendChild(opt0);
 open.forEach(j=>{const o=capEl('option','',j.title||j.id);o.value=j.id;sel.appendChild(o)});
 sel.value=capJobObj()?CAPJOB:'';
 const s=capSchema(),treeBox=$('capTree'),finBtn=$('capFin'),prog=$('capProg');
 $('formCard').classList.toggle('hide',!!(capJobObj()&&s));
 if(!capJobObj()||!s||!CAPST){treeBox.textContent='';prog.textContent='';finBtn.classList.add('hide');$('capMiss').classList.add('hide');return}
 finBtn.classList.remove('hide');const t=capTree(s,CAPST,[]),pr=capProgress(t);
 prog.textContent=(T.capDoneOf||'{done} of {total}').replace('{done}',pr.done).replace('{total}',pr.total);
 finBtn.textContent=CAPST.closed?(T.capReopen||'Reopen job'):(T.capFinish||'Finish job');
 treeBox.textContent='';
 function renderSlot(it,depth,repNode){
  const wrap=capEl('div','cnode'+(depth>0?' sub':''));wrap.dataset.k=it.k;
  const row=capEl('div','crow'),btn=capEl('button','cbtn');btn.type='button';
  const m=capEl('span','cmeta');
  if(it.photos[1]>0||it.photo_count>0)m.appendChild(capEl('span','','\u2066'+(it.photos[0]>0?it.photo_count+'/'+it.photos[0]:it.photo_count)+'\u2069'));
  if(!it.req)m.appendChild(capEl('span','',T.capOpt||'Optional'));
  m.appendChild(capEl('span','cst '+(it.skipped?'sk':it.done?'ok':'no'),it.skipped?'⏭':it.done?'✓':'✗'));
  btn.append(capEl('span','',it.label),m);btn.onclick=()=>capOpen(it.k);row.appendChild(btn);
  if(repNode&&it.iid){const delB=capEl('button','ghost cdel',T.capRemove||'Remove');delB.type='button';
   delB.disabled=repNode.instance_count<=(repNode.repeat.min||0);
   delB.onclick=e=>{e.stopPropagation();capRemove(repNode.node,it.iid)};row.appendChild(delB)}
  wrap.appendChild(row);for(const ch of it.children||[])wrap.appendChild(renderItem(ch,depth+1));return wrap}
 function renderItem(it,depth){if(it.kind!=='repeat')return renderSlot(it,depth,null);
  const box=capEl('div','cnode'+(depth>0?' sub':''));box.dataset.k=it.k;
  const hdr=capEl('div','crow'),btn=capEl('button','cbtn');btn.type='button';const m=capEl('span','cmeta');
  m.append(capEl('span','','\u2066'+it.instance_count+'/'+it.repeat.min+'\u2069'),
   capEl('span','cst '+(it.skipped?'sk':it.done?'ok':'no'),it.skipped?'⏭':it.done?'✓':'✗'));
  btn.append(capEl('span','',it.label),m);btn.onclick=()=>capOpen(it.k);hdr.appendChild(btn);box.appendChild(hdr);
  for(const sub of it.instances)box.appendChild(renderSlot(sub,depth+1,it));
  const addB=capEl('button','ghost cadd',(T.capAdd||'+ Add {name}').replace('{name}',it.label));
  addB.type='button';addB.disabled=it.instance_count>=(it.repeat.max||200);
  addB.onclick=()=>capAdd(it.node,it.iid);box.appendChild(addB);return box}
 for(const r of t.roots)treeBox.appendChild(renderItem(r,0));
 if(CAPSHEET)capRefreshSheet(t)}
function capFindItem(t,k){let hit=null;
 const walk=it=>{if(hit)return;if(it.k===k){hit=it;return}for(const s of it.instances||[])walk(s);for(const c of it.children||[])walk(c)};
 for(const r of t.roots)walk(r);return hit}
function capRefreshSheet(t){if(!CAPSHEET||!CAPST)return;const s=capSchema();if(!s)return;
 t=t||capTree(s,CAPST,[]);const it=capFindItem(t,CAPSHEET.k);if(!it){capCloseSheet();return}
 CAPSHEET=it;$('capPath').textContent=it.path_label;const shBtn=$('capShoot');
 if(it.kind==='repeat')shBtn.classList.add('hide');
 else{shBtn.classList.remove('hide');const pmin=it.photos[0]||0,have=it.photo_count||0;
  shBtn.textContent=pmin>0?(T.capTakeN||'Take a photo ({have}/{min})').replace('{have}',have).replace('{min}',pmin):(T.capTake||T.shoot||'Take a photo')}
 $('capSkipBtn').textContent=it.own_skip?(T.capClearSkip||'Clear skip'):(T.capCannot||'Cannot do this item')}
function capOpen(k){const s=capSchema();if(!s||!CAPST)return;
 const t=capTree(s,CAPST,[]),it=capFindItem(t,k);if(!it)return;CAPSHEET=it;
 const vmap={},qDefs=it.q||[],curAns=(CAPST.ans&&CAPST.ans[k])||{};
 for(const q of qDefs)if(q.name in curAns)vmap[q.name]=Array.isArray(curAns[q.name])?curAns[q.name][0]:curAns[q.name];
 CAPSHEET.vmap=vmap;
 drawFormIn($('capForm'),qDefs,vmap,(qname,val)=>{
  if(!CAPST)return;const now=Date.now();
  if(val===undefined||(Array.isArray(val)&&!val.length)){if(CAPST.ans[k]){delete CAPST.ans[k][qname];if(!Object.keys(CAPST.ans[k]).length)delete CAPST.ans[k]}}
  else{const q=qDefs.find(x=>x.name===qname),cv=q?capCleanVal(q,val):val;(CAPST.ans[k]||(CAPST.ans[k]={}))[qname]=[cv!==undefined?cv:val,now]}
  capTouch();const it2=capFindItem(capTree(s,CAPST,[]),k);if(it2)$('capPath').textContent=it2.path_label});
 $('lCapNote').textContent=T.note||'Note (optional)';$('capNote').placeholder=T.note||'';
 const np=CAPST.note&&CAPST.note[k];$('capNote').value=np?(Array.isArray(np)?np[0]:np):'';
 $('capNote').oninput=()=>{if(!CAPST)return;const v=$('capNote').value.trim();if(v)CAPST.note[k]=[v,Date.now()];else delete CAPST.note[k];capTouch()};
 $('lCapSkip').textContent=T.capCannot||'Cannot do this item';$('capReason').placeholder=T.capReason||'Reason (at least 3 characters)';
 $('capReason').value=it.own_skip||'';$('capDoneBtn').textContent=T.capDone||'Done';
 capRefreshSheet(t);$('capSheet').classList.remove('hide')}
function capCloseSheet(){CAPSHEET=null;$('capSheet').classList.add('hide');capDraw()}
function capSkip(){if(!CAPSHEET||!CAPST)return;const k=CAPSHEET.k,r=$('capReason').value.trim();
 if(CAPST.skip[k]&&(!r||r===CAPST.skip[k][0])){delete CAPST.skip[k];$('capReason').value='';capTouch();capDraw();return}
 if(r.length<3){toast(T.capReasonNeed||'Write a reason of at least 3 characters.');$('capReason').focus();return}
 CAPST.skip[k]=[r,Date.now()];capTouch();capCloseSheet()}
function capShot(){if(!CAPSHEET||!CAPST||CAPSHEET.kind==='repeat')return;
 if((typeof iosBlocked!=='undefined'&&iosBlocked)||(typeof quotaStopped!=='undefined'&&quotaStopped)){
  if(quotaStopped)toast(T.quotaStop||'Phone storage is full.');return}
 const sf=shotFix();if(!sf){toast(T.waiting);return}if(!requireName())return;
 const qDefs=CAPSHEET.q||[],checked=checkForm(qDefs,CAPSHEET.vmap||{},$('capForm'));if(!checked)return;
 const now=Date.now(),k=CAPSHEET.k;
 for(const q of qDefs)if(q.name in checked)(CAPST.ans[k]||(CAPST.ans[k]={}))[q.name]=[checked[q.name],now];
 const it=capFindItem(capTree(capSchema(),CAPST,[]),k)||CAPSHEET;
 CAPSEL={j:CAPJOB,k:k,pl:it.path_label,ch:it.ch||[]};if(navigator.vibrate)navigator.vibrate(25);
 pending={fix:sf,compass:compass,at:now,f:{},cap:CAPSEL,note:($('capNote').value||'').trim()};$('file').click()}
function capFinish(){const s=capSchema();if(!s||!CAPST)return false;const box=$('capMiss');
 if(CAPST.closed){CAPST.closed=null;box.classList.add('hide');capTouch();capDraw();return true}
 const miss=capMissing(s,CAPST,[]);
 if(miss.length){box.textContent='';
  box.appendChild(capEl('div','',T.capMissingNeed||'Complete every required item or give a reason before finishing.'));
  const ul=capEl('ul','');
  miss.forEach(m=>{const why=m.reason==='repeat_min'
   ?(T.capNeedInst||'needs {have}/{min}').replace('{have}',m.have).replace('{min}',m.min)
   :[m.need_photos?(T.capNeedPhotos||'needs {n} more photo(s)').replace('{n}',m.need_photos):'',
     m.missing_q&&m.missing_q.length?(T.capNeedQ||'missing required answers'):''].filter(Boolean).join(', ');
   ul.appendChild(capEl('li','',m.path_label+(why?' — '+why:'')))});
  box.appendChild(ul);box.classList.remove('hide');toast(T.capMissingNeed||'Complete every required item or give a reason before finishing.');return false}
 box.classList.add('hide');CAPST.closed=Date.now();capTouch();capDraw();toast(T.capClosed||'Job finished on this phone');return true}
function capLabel(){if($('lCap'))$('lCap').textContent=T.capJobs||'Capture job';
 if($('lCapNote'))$('lCapNote').textContent=T.note||'Note (optional)';
 if($('lCapSkip'))$('lCapSkip').textContent=T.capCannot||'Cannot do this item';
 if($('capReason'))$('capReason').placeholder=T.capReason||'Reason (at least 3 characters)';
 if($('capDoneBtn'))$('capDoneBtn').textContent=T.capDone||'Done';
 capDraw()}
$('capJob').onchange=()=>capSelectJob($('capJob').value);$('capFin').onclick=capFinish;$('capShoot').onclick=capShot;
$('capSkipBtn').onclick=capSkip;$('capDoneBtn').onclick=capCloseSheet;$('capSheet').onclick=e=>{if(e.target===$('capSheet'))capCloseSheet()};
/*}CAP*/
async function flush(){if(busy)return;busy=true;
 let arrived=0;try{const allItems=(await all()).filter(x=>itemSt(x)<1).sort((a,b)=>a.meta.t-b.meta.t),
   rOnly=typeof relayOnly==='function'&&relayOnly(),items=rOnly?allItems.filter(x=>!x.prevSent&&!x.carried):allItems;
  let tried=0;for(const it of items){
   const res=await sendItem(it),st=typeof res==='object'&&res?res.status:res;
   if(!st)continue;   /* 0: another way (Google Drive) is sending this one */
   tried++;
   if(st===200){
    const rc=(typeof res==='object'&&res&&typeof res.c==='number')?res.c:(NL?1:2);
    const rh=(typeof res==='object'&&res&&res.h)||'';
    if(NL&&rh&&it.h&&rh!==it.h){continue}
    if(it.cap){if(!NL||rc>=2)await del(it.id);else await setState(it.id,2,rc)}
    else if(!NL||rc>=2){await del(it.id);sent++;arrived++;mark(it.id,NL?'✓✓':'✓')}
    else{await setState(it.id,2,rc);sent++;arrived++;if(typeof copyPoll==='function')setTimeout(copyPoll,1500)}}
   else if(st===202){if(it.cap)await del(it.id);else if(await prevDone(it.id))mark(it.id,'◐')}
   else if(st===403){
    const why=(typeof res==='object'&&res&&res.reason)||T.blocked||'403';
    if(it.cap||!NL){await del(it.id);if(!it.cap)mark(it.id,'✗',why)}
    else await setState(it.id,9,0,why);
    status(false,'blocked')}
   else if(st===400||st===413||(st>=400&&st<500&&st!==429)){
    const why=(typeof res==='object'&&res&&res.reason)||('HTTP '+st);
    if(it.cap||!NL){await del(it.id);if(!it.cap)mark(it.id,'✗',why)}
    else await setState(it.id,9,0,why)}
   else throw new Error(st);count()}
  const ok=tried?true:await alive();
  if(ok&&!rOnly)status(true,'connected');else if(!ok)offline()}catch(e){offline()}
 if(arrived){toast(T.arrived.replace('{n}',arrived));if(navigator.vibrate)navigator.vibrate([20,40,20])}
 busy=false;count()}
setInterval(flush,5000);window.addEventListener('online',flush);
setInterval(()=>{if(typeof copyPoll==='function')copyPoll()},30000);
async function checkEod(force){
 const a=(await all()).filter(x=>itemSt(x)!==3),posN=(typeof LAD==='object'&&LAD._items)?LAD._items.length:0;
 const pend=a.length+posN;
 if(!pend){if($('eodSheet'))$('eodSheet').classList.add('hide');return false}
 if(!force){const d=new Date(NOW());if(d.getHours()<EOD_HOUR)return false;
  const dk='biruni-eod-shown-'+ROOM+'-'+d.getFullYear()+'-'+(d.getMonth()+1)+'-'+d.getDate();
  if(kept(dk)==='1')return false;keep(dk,'1')}
 const s0=a.filter(x=>itemSt(x)===0).length,s1=a.filter(x=>itemSt(x)===1).length,
  s2=a.filter(x=>itemSt(x)===2).length,s9=a.filter(x=>itemSt(x)===9).length,parts=[];
 if(s0)parts.push((T.waitingPhotos||'{n} photos waiting').replace('{n}',s0));
 if(s1)parts.push((T.countCloud||'☁️ {n} in the cloud, waiting for the office').replace('{n}',s1));
 if(s2)parts.push((T.countOffice||'{n} at the office, waiting for 2nd copy').replace('{n}',s2));
 if(s9)parts.push((T.countRefused||'{n} refused — kept on this phone').replace('{n}',s9));
 if(posN)parts.push((T.ladKeep||'{n} positions kept on the phone').replace('{n}',posN));
 if($('eodList'))$('eodList').textContent=parts.join(' · ')||(T.eodPending||'{n} items still waiting for 2 copies').replace('{n}',pend);
 if($('eodSheet'))$('eodSheet').classList.remove('hide');return true}
setInterval(()=>checkEod(false),60000);
function thumb(it,s){const e=$('list').querySelector('.empty');if(e)e.remove();
 const d=document.createElement('div'),i=document.createElement('img'),sp=document.createElement('span');
 const u=URL.createObjectURL(it.blob);d.dataset.id=it.id;i.src=u;i.alt='';sp.className='badge';d.append(i,sp);
 $('list').prepend(d);mark(it.id,s);while($('list').children.length>9)$('list').lastChild.remove();
 const l=document.createElement('img');l.src=u;l.alt='';$('last').replaceChildren(l)}
function onFix(p){const c=p.coords,first=!fix;fix={lat:c.latitude,lon:c.longitude,acc:c.accuracy,alt:c.altitude,
 heading:c.heading,speed:c.speed,t:p.timestamp};if(!room)showFix();const a=Math.round(c.accuracy),q=a<=8?'good':a<=20?'fair':'poor';
 if(avg)avg.pts.push({lat:c.latitude,lon:c.longitude,acc:c.accuracy});tellPos();drawGuide();
 $('accBar').className=q;$('accBar').style.width=Math.max(6,Math.min(100,100-(a-3)*2.2))+'%';
 if(compass==null&&c.heading!=null&&!isNaN(c.heading))showDir(c.heading);
 const blk=(typeof iosBlocked!=='undefined'&&iosBlocked)||(typeof quotaStopped!=='undefined'&&quotaStopped);
 $('shot').disabled=blk;$('shotHint').textContent=quotaStopped?(T.quotaStop||''):T.shoot;if(first&&navigator.vibrate)navigator.vibrate(15)}
let gdraw=0;function showFix(){if(!fix)return;const a=Math.round(fix.acc),q=a<=8?'good':a<=20?'fair':'poor';
 $('pos').classList.remove('inroom');$('pos').textContent=fix.lat.toFixed(6)+', '+fix.lon.toFixed(6);$('acc').textContent='± '+a+' m';$('acc').className='acc '+q}
function showDir(h){$('arrow').style.transform='rotate('+Math.round(h)+'deg)';$('deg').textContent=Math.round(h)%360+'°';
 if(Date.now()-gdraw>250){gdraw=Date.now();drawGuide()}}
function onCompass(e){let h=null;if(typeof e.webkitCompassHeading==='number')h=e.webkitCompassHeading;
 else if(e.absolute&&e.alpha!=null)h=(360-e.alpha)%360;if(h!=null){compass=Math.round(h)%360;showDir(compass)}}
async function wake(){try{await navigator.wakeLock.request('screen')}catch(e){}}
async function start(){if(typeof iosBlocked!=='undefined'&&iosBlocked)return;
 if(!window.isSecureContext||!navigator.geolocation){status(false,'insecure');return}
 try{if(window.DeviceOrientationEvent&&DeviceOrientationEvent.requestPermission)await DeviceOrientationEvent.requestPermission()}catch(e){}
 window.addEventListener('deviceorientationabsolute',onCompass);window.addEventListener('deviceorientation',onCompass);
 navigator.geolocation.watchPosition(onFix,e=>status(false,T.denied+' ('+e.message+')'),
  {enableHighAccuracy:true,maximumAge:0,timeout:30000});wake();started=true;document.body.classList.add('working');
 $('intro').classList.add('hide');$('work').classList.remove('hide');$('bar').classList.remove('hide');flush();pollGuide();drawGuide()}
setInterval(()=>tellPos(true),30000);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'){if(started)wake();flush()}else checkEod(true)});
if($('eodBtn'))$('eodBtn').onclick=()=>{if($('eodSheet'))$('eodSheet').classList.add('hide');flush();if(typeof copyPoll==='function')copyPoll();if(typeof drivePump==='function')drivePump()};
if($('eodSheet'))$('eodSheet').onclick=e=>{if(e.target===$('eodSheet'))$('eodSheet').classList.add('hide')};
$('go').onclick=start;$('giveBtn').onclick=handGive;$('takeBtn').onclick=handTake;
$('handCancel').onclick=()=>{if(HAND)HAND.done=true;handClose()};if(!window.RTCPeerConnection)$('handCard').classList.add('hide');$('roomBtn').onclick=openScan;$('roomOff').onclick=()=>setRoom(null);
$('roomFind').oninput=roomList;$('scanSheet').onclick=e=>{if(e.target===$('scanSheet'))closeScan()};
window.addEventListener('resize',()=>drawRoom());$('avgBtn').onclick=toggleAvg;
function requireName(){
 if(SURVNAME){$('me').value=SURVNAME;$('me').readOnly=true;keep('biruni-me',SURVNAME);if($('meCard'))$('meCard').classList.remove('bad');return SURVNAME}
 let v=($('me').value||'').trim();
 if(!v){v=(kept('biruni-me')||'').trim();if(v)$('me').value=v}
 if(v){keep('biruni-me',v);if($('meCard'))$('meCard').classList.remove('bad');return v}
 const c=$('meCard');if(c){c.classList.remove('bad');void c.offsetWidth;c.classList.add('bad');
  c.scrollIntoView({behavior:'smooth',block:'center'})}
 $('me').focus();toast(T.needName||T.need||T.me);if(navigator.vibrate)navigator.vibrate([40,60,40]);return ''}
$('me').value=SURVNAME||kept('biruni-me')||'';if(SURVNAME)$('me').readOnly=true;
$('me').oninput=()=>{if($('me').value.trim()&&$('meCard'))$('meCard').classList.remove('bad');keep('biruni-me',$('me').value.trim())};
$('me').onchange=()=>{keep('biruni-me',$('me').value.trim());if($('meCard'))$('meCard').classList.remove('bad');tellPos(true)};
$('share').checked=kept('biruni-share')!=='0';$('share').onchange=()=>{keep('biruni-share',$('share').checked?'1':'0');tellPos(true)};
$('shot').onclick=()=>{if((typeof iosBlocked!=='undefined'&&iosBlocked)||(typeof quotaStopped!=='undefined'&&quotaStopped)){
  if(quotaStopped)toast(T.quotaStop||'Phone storage is full.');return}
 const sf=shotFix();if(!sf)return;if(!requireName())return;
 const inJob=!!(capJobObj()&&capSchema());
 const f=inJob?{}:answers();if(!f)return;if(navigator.vibrate)navigator.vibrate(25);
 CAPSEL=inJob?{j:CAPJOB,k:'*',pl:'*',ch:[]}:null;
 pending={fix:sf,compass:compass,at:Date.now(),f:f,cap:CAPSEL};$('file').click()};
$('file').onchange=async()=>{const f=$('file').files[0];$('file').value='';if(!f||!pending)return;
 if((typeof iosBlocked!=='undefined'&&iosBlocked)||(typeof quotaStopped!=='undefined'&&quotaStopped)){pending=null;return}
 const who=requireName();if(!who)return;
 const p=pending.fix,capBind=pending.cap||CAPSEL,pnote=pending.note!=null?pending.note:$('note').value.trim();
 const blob=await fitPhoto(new Blob([await f.arrayBuffer()],{type:f.type||'image/jpeg'}));
 if(typeof checkStorageSpace==='function'&&!await checkStorageSpace(blob.size*2)){pending=null;return}
 const meta={id:uid(),lat:p.lat,lon:p.lon,acc:p.acc,alt:p.alt,heading:p.heading,compass:pending.compass,
  t:pending.at,fix_age:Math.max(0,Math.round((pending.at-p.t)/1000)),kind:kind,note:pnote,device:dev,device_id:DEVID,surveyor:who.slice(0,40),by:who.slice(0,40),sk:SURVKEY||undefined,f:pending.f,avg_n:p.n,avg_sd:p.sd,level:p.level,unit:p.unit};
 if(capBind){meta.cap=capBind;CAPLASTFIX={j:capBind.j,lat:p.lat,lon:p.lon,acc:p.acc};
  if(CAPST&&CAPST.job===capBind.j){const arr=CAPST.ph[capBind.k]||(CAPST.ph[capBind.k]=[]);
   if(!arr.includes(meta.id))arr.push(meta.id);capTouch();capDraw()}}
 const it={id:meta.id,blob:blob,meta:meta,room:ROOM};pending=null;CAPSEL=null;await put(it);thumb(it,'⏳');$('note').value='';if(!capBind)afterShot();
 toast(T.kept);count();flush();if(typeof drivePump==='function')drivePump()};
$('langBtn').onclick=()=>{const box=$('langs');box.textContent='';Object.keys(ALL).forEach(k=>{const b=document.createElement('button');
 b.type='button';b.textContent=NAMES[k]||k;b.lang=k.replace('_','-');b.classList.toggle('sel',k===LANG);
 b.onclick=()=>{keep('biruni-lang',k);setLang(k);$('sheet').classList.add('hide')};box.appendChild(b)});
 $('sheet').classList.remove('hide')};
$('sheet').onclick=e=>{if(e.target===$('sheet'))$('sheet').classList.add('hide')};
/* ---- Phase P2: iOS install gate, persistent storage, unsent banner & sealed export (.bfx) ---- */
const HEADROOM_BYTES=15*1024*1024,BFX_MAX_CHUNK=200*1024*1024;
var iosBlocked=false,persistRefused=false,quotaStopped=false,spaceFreeBytes=null,deferredPrompt=null;
function isIos(){const u=String(window.BIRUNI_UA!=null?window.BIRUNI_UA:(navigator.userAgent||''));
 const p=String(window.BIRUNI_PLATFORM!=null?window.BIRUNI_PLATFORM:(navigator.platform||''));
 const mt=window.BIRUNI_MAX_TOUCH!=null?window.BIRUNI_MAX_TOUCH:(navigator.maxTouchPoints||0);
 return /iPad|iPhone|iPod/.test(u)||(p==='MacIntel'&&mt>1)}
function isStandalone(){if(window.BIRUNI_STANDALONE!=null)return !!window.BIRUNI_STANDALONE;
 if(typeof navigator.standalone==='boolean')return navigator.standalone;
 return !!(window.matchMedia&&window.matchMedia('(display-mode: standalone)').matches)}
function hasBridgeKey(){return typeof KEYB==='string'&&/^[A-Za-z0-9_-]{43}$/.test(KEYB)}
function validateBridgeLink(raw){
 const s=String(raw||'').trim();if(!s)return null;
 let frag='';
 try{if(/^https?:\/\//i.test(s)){const u=new URL(s);
   if(u.origin!==location.origin&&!/^https:\/\/(photos\.)?biruni-gis\.com$/i.test(u.origin))return null;
   frag=(u.hash||'').replace(/^#/,'')}
  else if(s.startsWith('#'))frag=s.slice(1);
  else if(s.includes('#')){const idx=s.indexOf('#'),pre=s.slice(0,idx).trim();
   if(pre&&!/^(https:\/\/(photos\.)?biruni-gis\.com)?(\/p\/?)?$/i.test(pre))return null;
   frag=s.slice(idx+1)}
  else if(/^k=[A-Za-z0-9_-]{43}(&|$)/.test(s))frag=s;
  else return null}catch(e){return null}
 const hp=new URLSearchParams(frag),k=hp.get('k')||'';
 if(!/^[A-Za-z0-9_-]{43}$/.test(k))return null;
 const i=hp.get('i')||'';if(i&&!/^[A-Za-z0-9_-]{22}$/.test(i))return null;
 const pk=hp.get('pk')||'';if(pk&&!/^[\w-]{81,99}$/.test(pk))return null;
 const o=(hp.get('o')||'').trim().toLowerCase();if(o&&!/^[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.workers\.dev$/.test(o))return null;
 const t=hp.get('t')||'';if(t&&!/^[a-z0-9-]{3,80}$/.test(t))return null;
 return {hash:'#'+frag,k:k,i:i,pk:pk,d:hp.get('d')==='1',o:o,t:t,l:hp.get('l')||'',sk:hp.get('sk')||'',sn:hp.get('sn')||''}}
async function applyBridgeLink(raw){
 const v=validateBridgeLink(raw);
 const err=$('iosPasteErr');
 if(!v){if(err)err.textContent=T.iosPasteBad||'That link is not a valid Biruni field photos link.';
  if($('iosPaste')){$('iosPaste').classList.remove('bad');void $('iosPaste').offsetWidth;$('iosPaste').classList.add('bad')}
  return false}
 if(err)err.textContent='';
 if($('iosPaste'))$('iosPaste').classList.remove('bad');
 try{history.replaceState(null,'',v.hash)}catch(e){try{location.hash=v.hash}catch(x){}}
 keep('biruni-last-link',location.origin+location.pathname+v.hash);
 ROOM=v.k.slice(0,12);
 if(v.pk&&v.d){PCKEY=v.pk;keep('biruni-pckey-'+ROOM,v.pk)}
 if(v.o)keep('biruni-org-'+ROOM,v.o);
 if(v.t)keep('biruni-tun-'+ROOM,v.t);
 if(v.sk){keep('biruni-sk-'+ROOM,v.sk);if(v.sn){keep('biruni-sn-'+ROOM,v.sn);keep('biruni-me',v.sn);if($('me')){$('me').value=v.sn;$('me').readOnly=true}}}
 if(v.l&&pick(v.l))setLang(pick(v.l));
 if(typeof startBridge==='function')await startBridge(v.k);
 checkIosGate();
 return true}
function checkIosGate(){
 const ios=isIos(),sa=isStandalone(),bridgeMode=typeof KEYB!=='undefined',needKey=bridgeMode&&!hasBridgeKey();
 const blockInstall=ios&&!sa;
 const blockPaste=bridgeMode&&sa&&needKey;
 iosBlocked=blockInstall||blockPaste;
 if($('iosGate'))$('iosGate').classList.toggle('hide',!blockInstall);
 if($('iosPaste'))$('iosPaste').classList.toggle('hide',!blockPaste);
 if($('intro'))$('intro').classList.toggle('hide',started||iosBlocked);
 if($('work'))$('work').classList.toggle('hide',!started||iosBlocked);
 if($('bar'))$('bar').classList.toggle('hide',!started||iosBlocked);
 if($('go'))$('go').disabled=iosBlocked||quotaStopped||(bridgeMode&&(!hasBridgeKey()||!window.isSecureContext||!window.RTCPeerConnection));
 if($('shot'))$('shot').disabled=iosBlocked||quotaStopped||!shotFix();
 if($('capShoot'))$('capShoot').disabled=iosBlocked||quotaStopped;
 return !iosBlocked}
async function copyCurrentLink(){
 const u=location.href;
 try{if(navigator.clipboard&&navigator.clipboard.writeText)await navigator.clipboard.writeText(u);
  else{const ta=document.createElement('textarea');ta.value=u;document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove()}}catch(e){}
 toast(T.iosCopied||'Link copied. Open the installed app from your Home Screen.');return u}
if($('iosCopyBtn'))$('iosCopyBtn').onclick=copyCurrentLink;
if($('iosPasteBtn'))$('iosPasteBtn').onclick=()=>applyBridgeLink($('iosPasteIn')&&$('iosPasteIn').value);
window.addEventListener('beforeinstallprompt',e=>{
 if(isIos()||isStandalone())return;
 e.preventDefault();deferredPrompt=e;
 if($('androidBar'))$('androidBar').classList.remove('hide')});
if($('androidBtn'))$('androidBtn').onclick=async()=>{
 if(!deferredPrompt)return;const p=deferredPrompt;deferredPrompt=null;
 try{await p.prompt()}catch(e){}
 if($('androidBar'))$('androidBar').classList.add('hide')};
function fmtBytes(b){if(!(typeof b==='number'&&isFinite(b)&&b>=0))return '';
 if(b>=1073741824)return '\u2066'+(b/1073741824).toFixed(1)+' GB\u2069';
 return '\u2066'+Math.max(0,Math.round(b/1048576))+' MB\u2069'}
function updatePersistBanner(){const b=$('persistWarn');if(!b)return;
 b.textContent=T.persistWarn||'Browser storage is not marked persistent. Keep this page open or export a backup bundle before closing.';
 b.classList.toggle('hide',!persistRefused)}
function updateQuotaBanner(){const b=$('quotaWarn');if(!b)return;
 b.textContent=T.quotaStop||'Phone storage is full. Capture is stopped so queued items are not lost — connect to the office or export a backup bundle.';
 b.classList.toggle('red',quotaStopped);
 b.classList.toggle('hide',!quotaStopped)}
function updateSpaceInfo(){const el=$('spaceInfo');if(!el)return;
 el.textContent=spaceFreeBytes!=null?(T.spaceFree||'Free space on phone: {s}').replace('{s}',fmtBytes(spaceFreeBytes)):''}
function setQuotaStopped(on){quotaStopped=!!on;updateQuotaBanner();
 const blk=iosBlocked||quotaStopped;
 if($('shot'))$('shot').disabled=blk||!shotFix();
 if($('shotHint'))$('shotHint').textContent=quotaStopped?(T.quotaStop||''):shotFix()?T.shoot:T.waiting;
 if($('capShoot'))$('capShoot').disabled=blk;
 if(quotaStopped)toast(T.quotaStop||'Phone storage is full.')}
async function checkPersist(){
 let ok=true;
 if(window.BIRUNI_PERSISTED!=null){ok=!!window.BIRUNI_PERSISTED}
 else if(navigator.storage){
  try{if(typeof navigator.storage.persist==='function')await navigator.storage.persist()}catch(e){}
  try{if(typeof navigator.storage.persisted==='function')ok=!!await navigator.storage.persisted()}catch(e){ok=false}}
 persistRefused=!ok;updatePersistBanner();return ok}
async function checkStorageSpace(needBytes){
 needBytes=+needBytes||0;
 if(!navigator.storage||typeof navigator.storage.estimate!=='function')return !quotaStopped;
 try{const e=await navigator.storage.estimate();
  if(e&&typeof e.quota==='number'&&e.quota>0){
   const u=typeof e.usage==='number'?e.usage:0,free=Math.max(0,e.quota-u);
   spaceFreeBytes=free;updateSpaceInfo();
   if(free<HEADROOM_BYTES+needBytes){setQuotaStopped(true);return false}
   if(quotaStopped&&free>=HEADROOM_BYTES*2)setQuotaStopped(false)}}catch(e){}
 return !quotaStopped}
async function updateUnsentBanner(){
 const b=$('unsentWarn');if(!b)return {n:0,days:0,level:''};
 const a=(await all()).filter(x=>{const s=itemSt(x);return s<3||s===9});
 if(!a.length){b.classList.add('hide');b.classList.remove('amber','red');return {n:0,days:0,level:''}}
 const now=NOW();let oldest=now;
 for(const x of a){const t=+(x.t0||(x.meta&&x.meta.t)||x.at||now);if(t>0&&t<oldest)oldest=t}
 const days=Math.floor(Math.max(0,now-oldest)/86400000);
 if(days<3){b.classList.add('hide');b.classList.remove('amber','red');return {n:a.length,days:days,level:''}}
 const level=days>=5?'red':'amber';
 b.textContent=(T.unsentBanner||'{n} items not protected for {d} days').replace('{n}',a.length).replace('{d}',days);
 b.classList.remove('hide','amber','red');b.classList.add(level);
 return {n:a.length,days:days,level:level}}
async function buildBundles(maxChunkBytes){
 const maxB=+maxChunkBytes||BFX_MAX_CHUNK;
 await sealWaiting();
 const raw=(await all()).filter(x=>x&&x.blob&&(itemSt(x)<3||itemSt(x)===9));
 const ready=[];
 for(const x of raw){let it=x;
  if(!(it.meta&&it.meta.sealed)){if(!PCKEY)continue;it=await put(it)}
  if(!(it.meta&&it.meta.sealed)||!it.blob)continue;
  const buf=new Uint8Array(await it.blob.arrayBuffer());
  const h=it.h||await blobSha(new Blob([buf]));
  ready.push({id:String(it.id),size:buf.length,sha:h,st:itemSt(it),bytes:buf})}
 if(!ready.length)return [];
 const groups=[];let cur=[],curBytes=0;
 for(const r of ready){
  if(cur.length&&curBytes+r.size+256>maxB){groups.push(cur);cur=[];curBytes=0}
  cur.push(r);curBytes+=r.size+128}
 if(cur.length)groups.push(cur);
 const ts=new Date(NOW()).toISOString().slice(0,10).replace(/-/g,'');
 const out=[];
 for(let i=0;i<groups.length;i++){
  const g=groups[i];
  const idx={v:1,items:g.map(r=>({id:r.id,size:r.size,sha:r.sha,st:r.st}))};
  const jb=new TextEncoder().encode(JSON.stringify(idx));
  const hdr=new Uint8Array(8);
  hdr.set([0x42,0x46,0x58,0x31],0);
  new DataView(hdr.buffer).setUint32(4,jb.length,false);
  const parts=[hdr,jb,...g.map(r=>r.bytes)];
  const name=groups.length>1?('biruni-'+ts+'-'+(i+1)+'.bfx'):('biruni-'+ts+'.bfx');
  out.push({name:name,blob:new Blob(parts,{type:'application/octet-stream'}),count:g.length,index:idx})}
 return out}
async function exportBundles(){
 const info=$('exportInfo'),links=$('exportLinks'),btn=$('exportBtn');
 if(btn)btn.disabled=true;
 if(info)info.textContent=T.exporting||'Packing sealed backup…';
 if(links)links.textContent='';
 try{const bundles=await buildBundles(BFX_MAX_CHUNK);
  if(!bundles.length){if(info)info.textContent=T.exportEmpty||'No sealed items are waiting on this phone.';
   toast(T.exportEmpty||'No sealed items are waiting on this phone.');return []}
  const total=bundles.reduce((s,b)=>s+b.count,0);
  if(links){for(const b of bundles){const a=document.createElement('a');
   a.href=URL.createObjectURL(b.blob);a.download=b.name;a.textContent='⬇ '+b.name+' ('+b.count+')';
   links.appendChild(a)}}
  let shared=false;
  if(typeof File==='function'&&navigator.share&&navigator.canShare){
   try{const files=bundles.map(b=>new File([b.blob],b.name,{type:'application/octet-stream'}));
    if(navigator.canShare({files:files})){await navigator.share({files:files,title:T.exportTitle||'Biruni backup'});shared=true}}catch(e){}}
  if(!shared&&links&&links.firstChild&&!window.BIRUNI_NO_AUTODL){try{links.firstChild.click()}catch(e){}}
  const msg=(T.exportDone||'Exported {n} sealed items ({f} file(s)). Nothing was deleted from this phone.').replace('{n}',total).replace('{f}',bundles.length);
  if(info)info.textContent=msg;toast(msg);return bundles}
 finally{if(btn)btn.disabled=false}}
if($('exportBtn'))$('exportBtn').onclick=exportBundles;
setLang(pick(kept('biruni-lang'))||pick(HASHL)||pick(DEF)||pick(navigator.language)||'en');
checkIosGate();
try{const c=JSON.parse(kept('biruni-cap-'+ROOM)||'null');if(c)capSet(c)}catch(e){}
try{const g=JSON.parse(kept('biruni-guide-'+ROOM)||'null');if(g)setGuide(g)}catch(e){}
{const e=document.createElement('div');e.className='empty';e.textContent=T.none;$('list').appendChild(e)}
(async()=>{probeSkew();db=await openDb();await checkPrivate();await checkPersist();await checkStorageSpace(0);await sealWaiting();if(typeof driveStart==='function')driveStart();const w=await all();w.filter(it=>!it.carried&&!it.cap).slice(-9).forEach(it=>thumb(it,badgeFor(it)));await count();flush()})();
</script></body></html>"""
