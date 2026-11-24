"""Account ownership and key holder tracking (no-loss P4).

Stores `<cert folder>/owners.json` with names and emails only (never any secret):
  {"v": 1, "org": str,
   "services": {sid: {"owner": str, "email": str, "at": str}},
   "key_backup": {"holder": str, "where": str},
   "left": {name: date}}
where `sid` is `drive`, `cloudflare` or `domain`.
"""
import json
import os
import time

FILE = "owners.json"
SERVICES = ("drive", "cloudflare", "domain")
PERSONAL_DOMAINS = frozenset((
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "hotmail.co.uk",
    "hotmail.fr",
    "outlook.com",
    "outlook.sa",
    "live.com",
    "msn.com",
    "yahoo.com",
    "yahoo.co.uk",
    "yahoo.fr",
    "ymail.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "gmx.com",
    "mail.com",
    "yandex.com",
    "zoho.com",
))


def is_personal(email):
    """True when `email` belongs to a consumer/personal webmail provider rather than an organisation domain."""
    s = str(email or "").strip().lower()
    if "@" not in s:
        return False
    dom = s.rsplit("@", 1)[1].strip()
    if not dom:
        return False
    if dom in PERSONAL_DOMAINS:
        return True
    base = dom.split(".", 1)[0]
    return base in ("gmail", "hotmail", "outlook", "yahoo", "icloud", "live", "protonmail")


def _empty():
    return {
        "v": 1,
        "org": "",
        "services": {
            sid: {"owner": "", "email": "", "at": ""}
            for sid in SERVICES
        },
        "key_backup": {"holder": "", "where": ""},
        "left": {},
    }


def _clean_doc(d):
    out = _empty()
    if not isinstance(d, dict):
        return out
    out["org"] = str(d.get("org") or "").strip()[:120]
    raw_srv = d.get("services")
    if isinstance(raw_srv, dict):
        for sid in SERVICES:
            e = raw_srv.get(sid)
            if isinstance(e, dict):
                out["services"][sid] = {
                    "owner": str(e.get("owner") or "").strip()[:80],
                    "email": str(e.get("email") or "").strip()[:120],
                    "at": str(e.get("at") or "").strip()[:20],
                }
    kb = d.get("key_backup")
    if isinstance(kb, dict):
        out["key_backup"] = {
            "holder": str(kb.get("holder") or "").strip()[:80],
            "where": str(kb.get("where") or "").strip()[:160],
        }
    left = d.get("left")
    if isinstance(left, dict):
        out["left"] = {
            str(k).strip()[:80]: str(v or "").strip()[:20]
            for k, v in left.items()
            if str(k).strip()
        }
    return out


def load(folder):
    """Load `<folder>/owners.json`; returns a normalized dict."""
    if not folder:
        return _empty()
    try:
        with open(os.path.join(folder, FILE), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    return _clean_doc(raw)


def save(folder, doc):
    """Atomically save `<folder>/owners.json` and return the cleaned document."""
    cleaned = _clean_doc(doc)
    if not folder:
        return cleaned
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return cleaned


def set_org(folder, org):
    doc = load(folder)
    doc["org"] = str(org or "").strip()[:120]
    return save(folder, doc)


def set_owner(folder, sid, owner, email="", at=None):
    """Set the owner name and email for service `sid` (`drive`, `cloudflare` or `domain`)."""
    if sid not in SERVICES:
        raise ValueError(f"unknown service: {sid}")
    doc = load(folder)
    doc["services"][sid] = {
        "owner": str(owner or "").strip()[:80],
        "email": str(email or "").strip()[:120],
        "at": str(at if at is not None else time.strftime("%Y-%m-%d")).strip()[:20],
    }
    return save(folder, doc)


def set_key_backup(folder, holder="", where=""):
    """Set who holds the password-locked key backup and where it is kept."""
    doc = load(folder)
    doc["key_backup"] = {
        "holder": str(holder or "").strip()[:80],
        "where": str(where or "").strip()[:160],
    }
    return save(folder, doc)


def mark_left(folder, name, date=None):
    """Record that `name` has left the organisation on `date` (YYYY-MM-DD)."""
    who = str(name or "").strip()[:80]
    if not who:
        return load(folder)
    doc = load(folder)
    # Remove any case-variant duplicate key first
    for k in list(doc["left"].keys()):
        if k.lower() == who.lower():
            doc["left"].pop(k, None)
    doc["left"][who] = str(date or time.strftime("%Y-%m-%d")).strip()[:20]
    return save(folder, doc)


def unmark_left(folder, name):
    who = str(name or "").strip().lower()
    doc = load(folder)
    for k in list(doc["left"].keys()):
        if k.lower() == who:
            doc["left"].pop(k, None)
    return save(folder, doc)


def has_left(doc, name):
    """Return the departure date string if `name` is in `doc['left']` (case-insensitive), else ''."""
    who = str(name or "").strip().lower()
    if not who or not isinstance(doc, dict):
        return ""
    for k, dt_str in (doc.get("left") or {}).items():
        if str(k).strip().lower() == who:
            return str(dt_str or "") or "1"
    return ""


def departures(doc, holders=()):
    """Check which active roles are held by someone marked in `doc['left']`.

    Returns a dict:
      {"services": [sid, ...],
       "holders": [(index_1_based, name), ...],
       "key_backup": holder_name_or_""}
    """
    doc = _clean_doc(doc)
    flagged_services = []
    for sid in SERVICES:
        owner = (doc["services"].get(sid) or {}).get("owner") or ""
        if owner and has_left(doc, owner):
            flagged_services.append(sid)
    flagged_holders = []
    for idx, h in enumerate(holders or (), start=1):
        name = str(h or "").strip()
        if name and has_left(doc, name):
            flagged_holders.append((idx, name))
    kb_holder = (doc.get("key_backup") or {}).get("holder") or ""
    kb_flagged = kb_holder if (kb_holder and has_left(doc, kb_holder)) else ""
    return {
        "services": flagged_services,
        "holders": flagged_holders,
        "key_backup": kb_flagged,
    }
