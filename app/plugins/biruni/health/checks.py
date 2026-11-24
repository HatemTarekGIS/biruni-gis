"""System health checks (no-loss P4): pure Python, no QGIS import."""
from dataclasses import dataclass
import datetime as dt
import json
import os
import time

from ..i18n import tr
from . import owners

LEVELS = ("ok", "warn", "bad", "na")
_RANK = {"na": 0, "ok": 1, "warn": 2, "bad": 3}

KEY_BACKUP_SETTING = "biruni/field-photos/key-backup"
MIRROR_FOLDER_SETTING = "biruni/field-photos/mirror-folder"
PHOTOS_FOLDER_SETTING = "biruni/field-photos/folder"
DRIVE_ROOTS_SETTING = "biruni/field-photos/drive-roots"
OWN_HOST_SETTING = "biruni/field-photos/own-host"
ORG_RELAY_SETTING = "biruni/bridge/own-relay"

DAY_S = 86400.0
KEY_BACKUP_WARN_DAYS = 180


@dataclass
class Ctx:
    """Health check context with no QGIS import."""
    get_setting: object
    cert_folder: str
    photos_folder: str
    now: float = 0.0

    def __post_init__(self):
        if not self.now:
            self.now = time.time()

    def setting(self, key, default=""):
        if callable(self.get_setting):
            try:
                v = self.get_setting(key, default)
                return default if v is None else v
            except TypeError:
                v = self.get_setting(key)
                return default if v is None else v
            except Exception:  # noqa: BLE001
                return default
        if isinstance(self.get_setting, dict):
            return self.get_setting.get(key, default)
        return default


_CHECKS = []


def register(fn, order=50):
    """Register a check function `fn(ctx)` returning a row dict or list of row dicts."""
    _CHECKS[:] = [(o, f) for o, f in _CHECKS if f is not fn]
    _CHECKS.append((int(order), fn))
    _CHECKS.sort(key=lambda pair: pair[0])
    return fn


def _norm_row(r, fallback_id="check"):
    if not isinstance(r, dict):
        return None
    lvl = str(r.get("level") or "na")
    if lvl not in LEVELS:
        lvl = "na"
    return {
        "id": str(r.get("id") or fallback_id),
        "group": str(r.get("group") or tr("System health")),
        "title": str(r.get("title") or r.get("id") or fallback_id),
        "level": lvl,
        "text": str(r.get("text") or ""),
        "fix": r.get("fix"),
    }


def run(ctx):
    """Run all registered health checks; any check that raises becomes an `na` row."""
    rows = []
    for _order, fn in list(_CHECKS):
        cid = getattr(fn, "check_id", None) or getattr(fn, "__name__", "check").lstrip("_")
        grp = getattr(fn, "check_group", None) or tr("System health")
        ttl = getattr(fn, "check_title", None) or cid
        try:
            res = fn(ctx)
        except Exception as e:  # noqa: BLE001 - a check that raises becomes na
            rows.append({
                "id": cid,
                "group": grp,
                "title": tr(ttl),
                "level": "na",
                "text": tr("Could not check this item ({e}).").format(e=e),
                "fix": None,
            })
            continue
        if res is None:
            continue
        items = res if isinstance(res, (list, tuple)) else [res]
        for item in items:
            nr = _norm_row(item, fallback_id=cid)
            if nr is not None:
                rows.append(nr)
    return rows


def worst(rows):
    """Return the worst level across `rows` (`bad` > `warn` > `ok` > `na`)."""
    if not rows:
        return "na"
    best = "na"
    for r in rows:
        lvl = r.get("level") if isinstance(r, dict) else str(r)
        if _RANK.get(lvl, 0) > _RANK.get(best, 0):
            best = lvl
    return best


def _parse_date_ts(s):
    s = str(s or "").strip()
    if not s:
        return None
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            d = dt.date.fromisoformat(s)
            return dt.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp()
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _current_kid(cert_folder):
    if not cert_folder:
        return ""
    try:
        from ..field_photos.handover import FILE as HK_FILE, HandoverKey

        # Only inspect or create when cert_folder is given
        if os.path.isfile(os.path.join(cert_folder, HK_FILE)):
            return HandoverKey(cert_folder).kid
        return HandoverKey(cert_folder).kid
    except Exception:  # noqa: BLE001
        return ""


def _check_key_backup(ctx):
    grp = tr("Keys and copies")
    title = tr("Office keys backup")
    raw = ctx.setting(KEY_BACKUP_SETTING, "")
    info = {}
    if isinstance(raw, dict):
        info = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                info = d
        except ValueError:
            info = {}
    kid = str(info.get("kid") or "").strip()
    v = int(info.get("v") or 0) if isinstance(info.get("v"), (int, float)) else 0
    at_str = str(info.get("at") or "").strip()
    cur_kid = _current_kid(ctx.cert_folder)

    if not kid or not at_str:
        return {
            "id": "key_backup",
            "group": grp,
            "title": title,
            "level": "bad",
            "text": tr("The office keys are not backed up yet. Make a password-protected key backup now."),
            "fix": "key_backup",
        }
    if v < 2:
        return {
            "id": "key_backup",
            "group": grp,
            "title": title,
            "level": "bad",
            "text": tr("The key backup is from an older version (v1) without QR codes and pins. Back up the keys again."),
            "fix": "key_backup",
        }
    if cur_kid and kid != cur_kid:
        return {
            "id": "key_backup",
            "group": grp,
            "title": title,
            "level": "bad",
            "text": tr("The key backup belongs to a different office key. Back up the current keys now."),
            "fix": "key_backup",
        }

    # Check if recipients.json was modified after the key backup date/time
    rec_path = os.path.join(ctx.cert_folder, "recipients.json") if ctx.cert_folder else ""
    at_ts = _parse_date_ts(at_str)
    if rec_path and os.path.isfile(rec_path):
        try:
            rec_mtime = os.path.getmtime(rec_path)
            # Also check if info has an explicit timestamp `ts` or compare against date
            bak_ts = float(info["ts"]) if isinstance(info.get("ts"), (int, float)) else at_ts
            if bak_ts is not None and rec_mtime > bak_ts + 1.0:
                return {
                    "id": "key_backup",
                    "group": grp,
                    "title": title,
                    "level": "warn",
                    "text": tr("The recipients list changed since the last key backup ({date}). Back up the keys again.").format(date=at_str),
                    "fix": "key_backup",
                }
            # If both happen on the same day in tests without explicit ts, check if recipients file is newer than backup day start
            if len(at_str) == 10:
                d0 = dt.date.fromisoformat(at_str)
                rec_day = dt.datetime.fromtimestamp(rec_mtime, dt.timezone.utc).date()
                if rec_day > d0:
                    return {
                        "id": "key_backup",
                        "group": grp,
                        "title": title,
                        "level": "warn",
                        "text": tr("The recipients list changed since the last key backup ({date}). Back up the keys again.").format(date=at_str),
                        "fix": "key_backup",
                    }
        except (OSError, ValueError):
            pass

    if at_ts is not None:
        age_days = (ctx.now - at_ts) / DAY_S
        if age_days > KEY_BACKUP_WARN_DAYS:
            return {
                "id": "key_backup",
                "group": grp,
                "title": title,
                "level": "warn",
                "text": tr("The key backup is {n} days old ({date}). Make a fresh key backup and check its password.").format(
                    n=int(age_days), date=at_str
                ),
                "fix": "key_backup",
            }

    own_doc = owners.load(ctx.cert_folder)
    kb = own_doc.get("key_backup") or {}
    if kb.get("holder") and owners.has_left(own_doc, kb["holder"]):
        return {
            "id": "key_backup",
            "group": grp,
            "title": title,
            "level": "bad",
            "text": tr("{name} holds the key backup and has left. Move the key backup to a current owner.").format(
                name=kb["holder"]
            ),
            "fix": "owners",
        }

    return {
        "id": "key_backup",
        "group": grp,
        "title": title,
        "level": "ok",
        "text": tr("Keys backed up (v2) on {date}.").format(date=at_str),
        "fix": None,
    }


_check_key_backup.check_id = "key_backup"
_check_key_backup.check_group = "Keys and copies"
_check_key_backup.check_title = "Office keys backup"


def _check_second_copy(ctx):
    grp = tr("Keys and copies")
    title = tr("Second copy folder")
    mf = str(ctx.setting(MIRROR_FOLDER_SETTING, "") or "").strip()
    if not mf:
        return {
            "id": "second_copy",
            "group": grp,
            "title": title,
            "level": "bad",
            "text": tr("No second copy folder is set. Choose a folder on another disk or drive so every photo has two copies."),
            "fix": "second_copy",
        }
    if not os.path.isdir(mf):
        return {
            "id": "second_copy",
            "group": grp,
            "title": title,
            "level": "bad",
            "text": tr("The second copy folder cannot be reached ({path}). Connect its disk or choose another folder.").format(
                path=mf
            ),
            "fix": "second_copy",
        }
    try:
        from ..field_photos.copies import different_volume

        if ctx.photos_folder and not different_volume(ctx.photos_folder, mf):
            return {
                "id": "second_copy",
                "group": grp,
                "title": title,
                "level": "warn",
                "text": tr("The second copy folder is on the same disk as the photos folder ({path}). Put it on another disk.").format(
                    path=mf
                ),
                "fix": "second_copy",
            }
    except Exception:  # noqa: BLE001
        pass
    return {
        "id": "second_copy",
        "group": grp,
        "title": title,
        "level": "ok",
        "text": tr("Second copy folder is ready ({path}).").format(path=mf),
        "fix": None,
    }


_check_second_copy.check_id = "second_copy"
_check_second_copy.check_group = "Keys and copies"
_check_second_copy.check_title = "Second copy folder"


def _load_emergency_holders(cert_folder):
    if not cert_folder:
        return False, []
    try:
        from ..field_photos import recipients

        d = recipients.load(cert_folder)
    except Exception:  # noqa: BLE001
        return False, []
    has_em = any(e.get("role") == "emergency" for e in (d.get("r") or []))
    em_info = d.get("emergency") if isinstance(d.get("emergency"), dict) else {}
    holders = [str(h or "").strip() for h in (em_info.get("holders") or []) if str(h or "").strip()]
    if not has_em and not holders:
        return False, []
    return True, holders


def _check_emergency_key(ctx):
    grp = tr("Keys and copies")
    title = tr("Emergency key (3 of 5)")
    has_em, holders = _load_emergency_holders(ctx.cert_folder)
    if not has_em:
        return {
            "id": "emergency_key",
            "group": grp,
            "title": title,
            "level": "warn",
            "text": tr("No 3-of-5 emergency key is set. Create one so 3 of 5 team members can open sealed files if the office key is lost."),
            "fix": "emergency_key",
        }
    own_doc = owners.load(ctx.cert_folder)
    out = [{
        "id": "emergency_key",
        "group": grp,
        "title": title,
        "level": "ok",
        "text": (tr("Emergency key holders: {names}.").format(names="، ".join(holders))
                 if holders else tr("Emergency key is active.")),
        "fix": None,
    }]
    for idx, h in enumerate(holders, start=1):
        left_on = owners.has_left(own_doc, h)
        if left_on:
            out.append({
                "id": f"holder:{idx}",
                "group": grp,
                "title": tr("Emergency share holder {n}: {name}").format(n=idx, name=h),
                "level": "bad",
                "text": tr("{name} holds share {n} of the emergency key and has left. Make a new emergency key with 5 current holders.").format(
                    name=h, n=idx
                ),
                "fix": "emergency_key",
            })
    return out


_check_emergency_key.check_id = "emergency_key"
_check_emergency_key.check_group = "Keys and copies"
_check_emergency_key.check_title = "Emergency key (3 of 5)"


def _check_surveyors(ctx):
    grp = tr("Surveyors")
    if not ctx.photos_folder:
        return []
    from ..field_photos.presence import Presence
    from ..field_photos.server import read_surveyor_keys

    pres = Presence(ctx.photos_folder)
    p_rows = {r["dev"]: r for r in pres.rows(ctx.now)}
    skeys = read_surveyor_keys(ctx.photos_folder).get("surveyors") or {}
    for sid, s in skeys.items():
        if s.get("revoked"):
            continue
        if sid not in p_rows:
            p_rows[sid] = {
                "dev": sid,
                "name": str(s.get("name") or sid),
                "heard": None,
                "pos_at": None,
                "q": None,
                "old": None,
                "lp": None,
            }
        elif not p_rows[sid].get("name") and s.get("name"):
            p_rows[sid]["name"] = str(s["name"])

    out = []
    now_ms = ctx.now * 1000.0
    for dev in sorted(p_rows, key=lambda d: ((p_rows[d].get("name") or "~").lower(), d)):
        r = p_rows[dev]
        name = r.get("name") or dev
        title = tr("Surveyor: {name}").format(name=name)
        cid = f"surveyor:{dev}"
        q = r.get("q")
        old_ms = r.get("old")
        if q is None and old_ms is None:
            out.append({
                "id": cid,
                "group": grp,
                "title": title,
                "level": "na",
                "text": tr("Not reported yet — waiting for the phone to connect."),
                "fix": "field_photos",
            })
            continue
        q_list = q if isinstance(q, list) and len(q) == 4 else [0, 0, 0, 0]
        n0, n1, n2, n9 = q_list
        pending = n0 + n1 + n2
        age_days = ((now_ms - float(old_ms)) / (DAY_S * 1000.0)) if (old_ms and old_ms > 0) else 0.0
        if age_days > 5.0:
            out.append({
                "id": cid,
                "group": grp,
                "title": title,
                "level": "bad",
                "text": tr("{name} has {n} items waiting for {d} days without two copies. Connect their phone or import a sealed bundle.").format(
                    name=name, n=max(pending, 1), d=int(age_days)
                ),
                "fix": "field_photos",
            })
        elif age_days > 3.0:
            out.append({
                "id": cid,
                "group": grp,
                "title": title,
                "level": "warn",
                "text": tr("{name} has {n} items waiting for {d} days. Ask them to open the page on Wi-Fi.").format(
                    name=name, n=max(pending, 1), d=int(age_days)
                ),
                "fix": "field_photos",
            })
        elif n9 > 0:
            out.append({
                "id": cid,
                "group": grp,
                "title": title,
                "level": "warn",
                "text": tr("{name} has {n} refused items kept on their phone. Check their QR code or import their sealed bundle.").format(
                    name=name, n=n9
                ),
                "fix": "field_photos",
            })
        else:
            out.append({
                "id": cid,
                "group": grp,
                "title": title,
                "level": "ok",
                "text": (tr("{name}: {n} pending items on the phone.").format(name=name, n=pending)
                         if pending else tr("{name}: all items have 2 verified copies.").format(name=name)),
                "fix": None,
            })
    return out


_check_surveyors.check_id = "surveyors"
_check_surveyors.check_group = "Surveyors"
_check_surveyors.check_title = "Surveyors"


SERVICE_TITLES = {
    "drive": "Google Drive account owner",
    "cloudflare": "Cloudflare account owner",
    "domain": "Domain owner",
}


def _check_owners(ctx):
    grp = tr("Account owners")
    doc = owners.load(ctx.cert_folder)
    services = doc.get("services") or {}
    out = []
    for sid in owners.SERVICES:
        info = services.get(sid) or {}
        owner_name = str(info.get("owner") or "").strip()
        email = str(info.get("email") or "").strip()
        title = tr(SERVICE_TITLES.get(sid, sid))
        cid = f"owner:{sid}"
        if not owner_name and not email:
            out.append({
                "id": cid,
                "group": grp,
                "title": title,
                "level": "warn",
                "text": tr("No owner is recorded for {service}. Record who manages this account using an organisation email.").format(
                    service=title
                ),
                "fix": f"owner:{sid}",
            })
            continue
        if owner_name and owners.has_left(doc, owner_name):
            out.append({
                "id": cid,
                "group": grp,
                "title": title,
                "level": "bad",
                "text": tr("{name} has left the organisation. Transfer {service} to a new owner and change its password.").format(
                    name=owner_name, service=title
                ),
                "fix": f"owner:{sid}",
            })
            continue
        if not owner_name or not email:
            out.append({
                "id": cid,
                "group": grp,
                "title": title,
                "level": "warn",
                "text": tr("Complete both the owner name and organisation email for {service}.").format(service=title),
                "fix": f"owner:{sid}",
            })
            continue
        if owners.is_personal(email):
            out.append({
                "id": cid,
                "group": grp,
                "title": title,
                "level": "warn",
                "text": tr("{email} is a personal email address. Move {service} to an organisation email.").format(
                    email=email, service=title
                ),
                "fix": f"owner:{sid}",
            })
            continue
        out.append({
            "id": cid,
            "group": grp,
            "title": title,
            "level": "ok",
            "text": f"{owner_name} ({email})",
            "fix": None,
        })
    return out


_check_owners.check_id = "owners"
_check_owners.check_group = "Account owners"
_check_owners.check_title = "Account owners"


register(_check_key_backup, order=10)
register(_check_second_copy, order=20)
register(_check_emergency_key, order=30)
register(_check_surveyors, order=40)
register(_check_owners, order=50)
