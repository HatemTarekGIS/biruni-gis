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
import datetime as dt
import json
import os
import queue
import re
import secrets
import ssl
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from ..i18n import tr
from ..local_server import ExclusiveServer

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
    when = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc) if t else dt.datetime.now(dt.timezone.utc)
    by = _text(m.get("surveyor") or m.get("by"), 40)
    dev_id = _text(m.get("device_id"), 64)
    if dev_id and not DEV_RE.match(dev_id):
        dev_id = ""
    route = str(m.get("route") or "")
    route = route if route in ("lan", "direct", "relay", "peer", "drive") else ("relay" if m.get("preview") else "")
    return {"id": m["id"], "lat": lat, "lon": lon, "acc": _num(m.get("acc"), 0),
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
    when = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc) if t else dt.datetime.now(dt.timezone.utc)
    net, eff, route = str(m.get("net") or ""), str(m.get("eff") or ""), str(m.get("route") or "")
    return {"id": m["id"], "name": _text(m.get("name"), 40), "lat": lat, "lon": lon, "acc": _num(m.get("acc"), 0),
            "heading": _num(m.get("heading"), 0, 360), "seen": when.isoformat(timespec="seconds"),
            "alt": _num(m.get("alt")), "speed": _num(m.get("speed"), 0, 200), "t": t,
            "sk": _text(m.get("sk"), 64),
            # the phone's link, for the team list: Wi-Fi or mobile data, the time to the computer and back
            # (ms; measured by the phone, or by this computer over the bridge), the route, the battery
            "net": net if net in NETS else "", "eff": eff if eff in ("slow-2g", "2g", "3g", "4g") else "",
            "down": _num(m.get("down"), 0, 10000), "ping": _num(m.get("ping"), 0, 120000),
            "crtt": _num(m.get("crtt"), 0, 60000), "route": route if route in ("lan", "direct", "relay", "peer") else "",
            "bat": _num(m.get("bat"), 0, 1), "chg": m.get("chg") is True}


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
            "surveyor", "device_id"}
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

    def __init__(self, folder, form=()):
        self.folder = folder
        self.form = clean_form(list(form))
        self.lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)
        self.records = {r["id"]: r for r in read_log(folder)}
        self.blocked = read_blocked(folder)
        self.surveyor_keys = read_surveyor_keys(folder)

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

    def save(self, meta, data):
        """(record, new) - the photo is on disk when this returns.
        When a preview arrived earlier over the relays and the full original arrives later with the same id,
        the original replaces the preview file in the same record."""
        ext = sniff(data)
        if ext is None:
            raise ValueError("not a photo")
        rec = clean_meta(meta, self.form)
        sk = _text(meta.get("sk"), 64) if isinstance(meta, dict) else ""
        with self.lock:
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
            old = self.records.get(rec["id"])
            if old is not None:
                if not old.get("preview") or rec.get("preview"):
                    return old, False
                if not rec.get("by") and old.get("by"):
                    rec["by"] = old["by"]
                    rec["surveyor"] = old.get("surveyor") or old["by"]
                if not rec.get("device_id") and old.get("device_id"):
                    rec["device_id"] = old["device_id"]
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
                self.records[rec["id"]] = rec
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
            self.records[rec["id"]] = rec
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
                 bind="0.0.0.0", form=()):
        from ..gps.browser import ensure_certificate

        self.token = secrets.token_urlsafe(12)
        self.port = port
        self.events = queue.Queue()
        self.store = PhotoStore(folder, form)
        self.cert, self.key = ensure_certificate(cert_folder, list(addresses))
        from .handover import HandoverKey

        self.handover = HandoverKey(cert_folder)   # photos handed phone to phone are sealed for this key
        self.page = render_page(page_texts, lang, kinds, self.store.form, self.handover.public)
        # the bridge (bridge.py) runs a hidden receiver page on this PC that hands the photos in here
        self.bridge_config = json.dumps({"lang": lang,
                                         "kinds": [k for k in (s.strip() for s in kinds) if k][:24],
                                         "form": self.store.form, "pk": self.handover.public},
                                        ensure_ascii=False).encode("utf-8")
        self._phones = set()
        self.guide_v = 0
        self.guide = guide_bytes(0, [])[0]
        server = self

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

            def _send(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body:
                    self.wfile.write(body)

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
                    self._send(200, b'{"ok":true}')
                elif route == "bridge" and self._local():
                    from .bridge import receiver_page

                    self._send(200, receiver_page(), "text/html; charset=utf-8")
                elif route == "guide":
                    self._send(200, server.guide)
                elif route == "bridge-config" and self._local():
                    self._send(200, server.bridge_config)
                else:
                    self._send(404)

            def _local(self):
                return self.client_address[0] == "127.0.0.1"

            def do_POST(self):  # noqa: N802
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
                        meta, data = server.handover.open(data)
                    else:
                        meta = raw_m
                    rec, new = server.store.save(meta, data)
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
                self._send(200, json.dumps({"ok": True, "id": rec["id"]}).encode())

        self.httpd = ExclusiveServer((bind, port), Handler)
        self.httpd.daemon_threads = True
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def set_guide(self, items):
        """Send these to every phone (they read it within seconds); returns how many went."""
        self._guide_items = list(items)
        return self._publish_guide()

    def set_indoor(self, payload):
        """The indoor map goes with the guide; None takes it away. Returns how many rooms went."""
        self._indoor = payload
        self._publish_guide()
        return len(payload["units"]) if payload else 0

    def _publish_guide(self):
        self.guide_v += 1
        self.guide, n = guide_bytes(self.guide_v, getattr(self, "_guide_items", []))
        indoor = getattr(self, "_indoor", None)
        if indoor:
            g = json.loads(self.guide)
            g["indoor"] = indoor
            self.guide = json.dumps(g, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return n

    def url(self, host):
        return f"https://{host}:{self.port}/{self.token}/"

    def path(self):
        return f"/{self.token}/"

    def stop(self):
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
            "driveRelink": tr("Link Google Drive again"), "driveOpen": tr("Open the Google Drive link"),
            "driveUnlinked": tr("When the computer cannot be reached, photos go to the office's Google Drive folder, "
                                "locked so that only the office computer can open them."),
            "driveIdle": tr("Linked. Photos go here when the computer cannot be reached."),
            "driveAuth": tr("The link to Google Drive has ended. Tap to link again."),
            "driveUp": tr("Sending to Google Drive…"), "driveWait": tr("Google Drive is busy - trying again soon."),
            "driveFull": tr("Your Google Drive is full. Photos wait on this phone."),
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
            "drivePickAgain": tr("Choose another folder"), "driveFolder": tr("Folder:")}


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
<meta name="color-scheme" content="light dark"><title>Biruni GIS</title>
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
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:17px;letter-spacing:-.01em}
.logo{width:30px;height:30px;border-radius:10px;background:var(--accent);color:var(--ink);display:grid;place-items:center;font-size:18px}
.tools{display:flex;gap:8px;align-items:center}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;padding:6px 11px;border-radius:999px;
 background:var(--card);border:1px solid var(--line);color:var(--dim);white-space:nowrap}
.pill.warn{border-color:var(--fair);color:var(--fair);font-weight:700}
.pill svg{font-size:15px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}
.dot.on{background:var(--good)}.dot.off{background:var(--poor)}.dot.wait{background:var(--fair);animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.3}}
.banner{margin:2px 16px 4px;padding:10px 13px;border-radius:14px;font-size:14px;line-height:1.45;
 background:var(--card);border:1px solid var(--line);border-inline-start:4px solid var(--fair)}
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
.badge.ok{background:var(--good)}.badge.bad{background:var(--poor)}
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
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head><body>
<header class="top">
 <div class="brand"><span class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="11" r="3"/><path d="M17.66 16.66L13.41 20.9a2 2 0 0 1-2.83 0l-4.24-4.24a8 8 0 1 1 11.32 0z"/></svg></span><span id="title">Biruni GIS</span></div>
 <div class="tools"><span class="pill warn hide" id="waitPill"></span><span class="pill" id="chip"><i class="dot wait" id="dot"></i><span id="chipText"></span></span>
 <button class="pill" id="langBtn" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3.6 9h16.8M3.6 15h16.8M11.5 3a17 17 0 0 0 0 18M12.5 3a17 17 0 0 1 0 18"/></svg><span id="langCode"></span></button></div>
</header>
<div class="banner hide" id="st"></div>
<div class="banner hide" id="privWarn"></div>
<main>
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
 <div class="card">
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
<div class="toast" id="toast"></div>
<script>
let ALL=__TEXTS__,KINDS=__KINDS__,FORM=__FORM__,ROOM='',HASHL=null,GPSMODE=false,T={},LANG='en',PCKEY='__PCKEY__';const DEF='__DEFLANG__',$=i=>document.getElementById(i);
const NAMES={en:'English',ar:'العربية',es:'Español',pt_BR:'Português',fr:'Français',tr:'Türkçe',fa:'فارسی'};
function pick(l){if(!l)return null;l=String(l).replace('-','_');if(ALL[l])return l;const b=l.split('_')[0].toLowerCase();
 return Object.keys(ALL).find(k=>k.split('_')[0].toLowerCase()===b)||null}
function keep(k,v){try{if(v==null)localStorage.removeItem(k);else localStorage.setItem(k,v)}catch(e){}}
function kept(k){try{return localStorage.getItem(k)}catch(e){return null}}
/*BOOT*/
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
 $('handCancel').textContent=T.handCancel;$('lSent').textContent=bare(T.sent);$('lWait').textContent=bare(T.queued);$('shotHint').textContent=fix?T.shoot:T.waiting;
 $('shot').setAttribute('aria-label',T.shoot);$('langBtn').setAttribute('aria-label',T.language);
 $('langCode').textContent=LANG.split('_')[0].toUpperCase();const e=$('list').querySelector('.empty');if(e)e.textContent=T.none;
 if(privMode&&$('privWarn'))$('privWarn').textContent=T.incognito||'';if(typeof count==='function')count();
 if(typeof driveLabel==='function')driveLabel()}
function setLang(l){LANG=l;T=Object.assign({},ALL.en||{},ALL[l]||{});const h=document.documentElement;
 h.lang=l.replace('_','-');h.dir=(l==='ar'||l==='fa')?'rtl':'ltr';label();drawKinds();drawForm();
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
function chips(f,box){const multi=f.type==='multi',opts=f.type==='yesno'?[[true,T.yes],[false,T.no]]:f.choices.map(c=>[c,c]);
 const on=v=>multi?(vals[f.name]||[]).includes(v):vals[f.name]===v;
 opts.forEach(([v,t])=>{const b=document.createElement('button');b.type='button';b.textContent=t;b.classList.toggle('sel',on(v));
  b.onclick=()=>{if(multi){const a=(vals[f.name]||[]).filter(x=>x!==v);if(!on(v))a.push(v);vals[f.name]=a}
   else vals[f.name]=on(v)?undefined:v;if(vals[f.name]===undefined||(multi&&!vals[f.name].length))delete vals[f.name];
   [...box.children].forEach((x,i)=>x.classList.toggle('sel',on(opts[i][0])));box.parentNode.classList.remove('bad');saveVals()};
  box.appendChild(b)})}
function drawForm(){const box=$('form');box.textContent='';FORM.forEach(f=>{const d=document.createElement('div');d.className='fld';
 d.dataset.name=f.name;const l=document.createElement('span');l.className='lbl';l.textContent=f.label;
 if(f.req){const r=document.createElement('span');r.className='req';r.textContent='*';r.title=T.required;l.appendChild(r)}d.appendChild(l);
 if(['choice','multi','yesno'].includes(f.type)){const c=document.createElement('div');c.className='kinds';chips(f,c);d.appendChild(c)}
 else{const i=document.createElement('input');i.className='in';i.autocomplete='off';i.enterKeyHint='next';
  i.type=f.type==='date'?'date':'text';if(f.type==='number')i.inputMode='decimal';if(f.type==='integer')i.inputMode='numeric';if(f.type!=='text')i.dir='ltr';
  if(f.type==='text')i.maxLength=250;
  if(f.min!=null||f.max!=null)i.placeholder=(f.min!=null?f.min:'')+' – '+(f.max!=null?f.max:'');
  i.value=vals[f.name]!=null?vals[f.name]:'';i.oninput=()=>{if(i.value==='')delete vals[f.name];else vals[f.name]=i.value;
   d.classList.remove('bad');saveVals()};d.appendChild(i)}
 box.appendChild(d)})}
/* checked before the camera opens: null and the field marked when something is missing or wrong */
function answers(){const out={};let first=null,why='';
 for(const f of FORM){let v=vals[f.name],ok=true;
  if(v==null||v===''||(Array.isArray(v)&&!v.length)){if(f.req){ok=false;why=why||'need'}}
  else if(f.type==='number'||f.type==='integer'){const n=Number(digits(v));
   if(digits(v)===''||!isFinite(n)||(f.type==='integer'&&!Number.isInteger(n))||(f.min!=null&&n<f.min)||(f.max!=null&&n>f.max)){
    ok=false;why=why||'bad'}else out[f.name]=n}
  else if(f.type==='text')out[f.name]=String(v).trim().slice(0,250);
  else out[f.name]=v;
  const e=document.querySelector('.fld[data-name="'+f.name+'"]');
  if(e){e.classList.remove('bad');if(!ok){void e.offsetWidth;e.classList.add('bad');first=first||e}}}
 if(first){first.scrollIntoView({behavior:'smooth',block:'center'});toast(T[why]);if(navigator.vibrate)navigator.vibrate([40,60,40]);return null}
 return out}
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
 try{await sendPos(Object.assign({id:DEVID,name:(SURVNAME||$('me').value).trim().slice(0,40),sk:SURVKEY||undefined,lat:f.lat,lon:f.lon,acc:f.acc,alt:f.alt,
  speed:f.speed,heading:compass!=null?compass:f.heading,t:f.t,level:f.level,unit:f.unit},netInfo()))}catch(e){}}
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
 if(!room){indoorFix=null;$('roomName').textContent='';if(!fix){$('shot').disabled=true;$('shotHint').textContent=T.waiting;
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
  const a=avgFix;avgFix=null;$('avgInfo').textContent='';return a}return fix}
/* photos not yet on the PC wait in the phone's browser storage, sealed with the PC's key (ECDH P-256 + AES-GCM) */
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
async function sealRecord(it){if(!PCKEY||!it||!it.meta||it.meta.sealed||!it.blob)return it;
 try{const orig=await sealItem({meta:it.meta,blob:it.blob});let prev=null;
  if(!it.carried&&!it.prevSent){const pb=await makePreview(it.blob);
   prev=await sealItem({meta:Object.assign({},it.meta,{preview:true}),blob:pb})}
  return {id:it.id,blob:orig,prev:prev,prevSent:!!it.prevSent,meta:{id:it.id,sealed:1,t:it.meta.t||Date.now()},
   room:it.room||ROOM,carried:!!it.carried,pk:PCKEY}}catch(e){return it}}   /* pk: the key it is sealed for (.bfp kid) */
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
  if(!it.meta.surveyor)it.meta.surveyor=it.meta.by||nm}
 it=await sealRecord(it);if(db){try{await tx('readwrite',s=>s.put(it));return it}catch(e){}}
 mem=mem.filter(x=>x.id!==it.id);mem.push(it);return it}
async function all(){let a=[];if(db){try{a=await tx('readonly',s=>s.getAll())||[]}catch(e){}}
 return a.concat(mem).filter(x=>(x.room||'')===ROOM)}
async function sealWaiting(){if(!PCKEY)return;const a=await all();
 for(const x of a)if(x.meta&&!x.meta.sealed)await put(x)}
async function del(id){mem=mem.filter(x=>x.id!==id);if(db){try{await tx('readwrite',s=>s.delete(id))}catch(e){}}}
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
 if(on){b.classList.add('hide')}else{b.textContent=T[key]||key;b.classList.remove('hide')}}
let tt=null;function toast(s){const t=$('toast');t.textContent=s;t.classList.add('show');clearTimeout(tt);
 tt=setTimeout(()=>t.classList.remove('show'),2200)}
function mark(id,s){const e=document.querySelector('[data-id="'+id+'"] .badge');if(!e)return;e.textContent=s;
 e.className='badge'+(s==='✓'?' ok':s==='✗'?' bad':'')}
async function count(){const a=await all(),n=a.length,p=a.filter(x=>x.prevSent).length,c=a.filter(x=>x.carried).length;
 $('nSent').textContent=sent;$('nWait').textContent=n;
 const wTxt=(T.waitingPhotos||'{n} photos waiting').replace('{n}',n-p),
  pTxt=(T.previewSent||'{n} arrived as preview, original waiting for Wi-Fi').replace('{n}',p),
  wt=p?(p===n?pTxt:wTxt+' · '+pTxt):(T.waitingPhotos||'{n} photos waiting').replace('{n}',n);
 if($('waitPill')){$('waitPill').textContent=wt;$('waitPill').classList.toggle('hide',!n)}
 $('lWait').textContent=n?wt:bare(T.queued);
 $('handInfo').textContent=c?T.handCarry.replace('{n}',c):'';
 $('waitBox').classList.toggle('warn',n>0)}
/*SEND{*/
/* the phone talks to the PC's own server: one request per photo */
/* every request has a time limit: a photo whose sending stalled (mobile data, the tunnel) waited for ever, the
   phone never tried again and said "Waiting to send" while the computer got nothing */
function within(ms){const c=new AbortController();setTimeout(()=>c.abort(),ms);return c.signal}
async function sendItem(it){return (await fetch('photo?m='+encodeURIComponent(JSON.stringify(it.meta)),{method:'POST',
 body:it.blob,headers:{'Content-Type':it.blob.type||'image/jpeg'},signal:within(60000+it.blob.size/20)})).status}
async function alive(){return (await fetch('ping',{cache:'no-store',signal:within(15000)})).ok}
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
async function sealItem(it){if(it.meta&&it.meta.sealed)return it.blob;   /* already sealed for the PC: kept as it is */
 const m=new TextEncoder().encode(JSON.stringify(it.meta)),img=new Uint8Array(await it.blob.arrayBuffer()),n=new Uint8Array(4);
 new DataView(n.buffer).setUint32(0,m.length);return new Blob([await sealFor(PCKEY,hcat(n,m,img))],{type:'application/octet-stream'})}
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
async function flush(){if(busy)return;busy=true;
 let arrived=0;try{const allItems=(await all()).sort((a,b)=>a.meta.t-b.meta.t),
   rOnly=typeof relayOnly==='function'&&relayOnly(),items=rOnly?allItems.filter(x=>!x.prevSent&&!x.carried):allItems;
  for(const it of items){
   const st=await sendItem(it);
   if(!st)continue;   /* 0: another way (Google Drive) is sending this one */
   if(st===200){await del(it.id);sent++;arrived++;mark(it.id,'✓')}
   else if(st===202){it.prevSent=true;it.prev=null;await put(it);mark(it.id,'◐')}
   else if(st===403){await del(it.id);mark(it.id,'✗');status(false,'blocked')}
   else if(st===400||st===413){await del(it.id);mark(it.id,'✗')}
   else throw new Error(st);count()}
  const ok=items.length?true:await alive();
  if(ok&&!rOnly)status(true,'connected');else if(!ok)offline()}catch(e){offline()}
 if(arrived){toast(T.arrived.replace('{n}',arrived));if(navigator.vibrate)navigator.vibrate([20,40,20])}
 busy=false;count()}
setInterval(flush,5000);window.addEventListener('online',flush);
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
 $('shot').disabled=false;$('shotHint').textContent=T.shoot;if(first&&navigator.vibrate)navigator.vibrate(15)}
let gdraw=0;function showFix(){if(!fix)return;const a=Math.round(fix.acc),q=a<=8?'good':a<=20?'fair':'poor';
 $('pos').classList.remove('inroom');$('pos').textContent=fix.lat.toFixed(6)+', '+fix.lon.toFixed(6);$('acc').textContent='± '+a+' m';$('acc').className='acc '+q}
function showDir(h){$('arrow').style.transform='rotate('+Math.round(h)+'deg)';$('deg').textContent=Math.round(h)%360+'°';
 if(Date.now()-gdraw>250){gdraw=Date.now();drawGuide()}}
function onCompass(e){let h=null;if(typeof e.webkitCompassHeading==='number')h=e.webkitCompassHeading;
 else if(e.absolute&&e.alpha!=null)h=(360-e.alpha)%360;if(h!=null){compass=Math.round(h)%360;showDir(compass)}}
async function wake(){try{await navigator.wakeLock.request('screen')}catch(e){}}
async function start(){if(!window.isSecureContext||!navigator.geolocation){status(false,'insecure');return}
 try{if(window.DeviceOrientationEvent&&DeviceOrientationEvent.requestPermission)await DeviceOrientationEvent.requestPermission()}catch(e){}
 window.addEventListener('deviceorientationabsolute',onCompass);window.addEventListener('deviceorientation',onCompass);
 navigator.geolocation.watchPosition(onFix,e=>status(false,T.denied+' ('+e.message+')'),
  {enableHighAccuracy:true,maximumAge:0,timeout:30000});wake();started=true;document.body.classList.add('working');
 $('intro').classList.add('hide');$('work').classList.remove('hide');$('bar').classList.remove('hide');flush();pollGuide();drawGuide()}
setInterval(()=>tellPos(true),30000);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'){if(started)wake();flush()}});
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
$('shot').onclick=()=>{if(!fix&&!indoorFix)return;if(!requireName())return;const f=answers();if(!f)return;if(navigator.vibrate)navigator.vibrate(25);
 pending={fix:shotFix(),compass:compass,at:Date.now(),f:f};$('file').click()};
$('file').onchange=async()=>{const f=$('file').files[0];$('file').value='';if(!f||!pending)return;
 const who=requireName();if(!who)return;
 const p=pending.fix,blob=new Blob([await f.arrayBuffer()],{type:f.type||'image/jpeg'});
 const meta={id:uid(),lat:p.lat,lon:p.lon,acc:p.acc,alt:p.alt,heading:p.heading,compass:pending.compass,
  t:pending.at,fix_age:Math.max(0,Math.round((pending.at-p.t)/1000)),kind:kind,note:$('note').value.trim(),device:dev,device_id:DEVID,surveyor:who.slice(0,40),by:who.slice(0,40),sk:SURVKEY||undefined,f:pending.f,avg_n:p.n,avg_sd:p.sd,level:p.level,unit:p.unit};
 const it={id:meta.id,blob:blob,meta:meta,room:ROOM};pending=null;await put(it);thumb(it,'⏳');$('note').value='';afterShot();
 toast(T.kept);count();flush()};
$('langBtn').onclick=()=>{const box=$('langs');box.textContent='';Object.keys(ALL).forEach(k=>{const b=document.createElement('button');
 b.type='button';b.textContent=NAMES[k]||k;b.lang=k.replace('_','-');b.classList.toggle('sel',k===LANG);
 b.onclick=()=>{keep('biruni-lang',k);setLang(k);$('sheet').classList.add('hide')};box.appendChild(b)});
 $('sheet').classList.remove('hide')};
$('sheet').onclick=e=>{if(e.target===$('sheet'))$('sheet').classList.add('hide')};
setLang(pick(kept('biruni-lang'))||pick(HASHL)||pick(DEF)||pick(navigator.language)||'en');
try{const g=JSON.parse(kept('biruni-guide-'+ROOM)||'null');if(g)setGuide(g)}catch(e){}
{const e=document.createElement('div');e.className='empty';e.textContent=T.none;$('list').appendChild(e)}
(async()=>{db=await openDb();await checkPrivate();await sealWaiting();if(typeof driveStart==='function')driveStart();const w=await all();w.filter(it=>!it.carried).slice(-9).forEach(it=>thumb(it,'⏳'));count();flush()})();
</script></body></html>"""
