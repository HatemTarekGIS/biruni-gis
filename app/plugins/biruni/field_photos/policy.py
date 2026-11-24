"""Signed tracking policy (no-loss P5: BRPL1).

The office can set a room-bound tracking policy (default mode, optional weekly work hours,
per-surveyor overrides and per-open-job overrides). Until the office saves a policy (seq=0),
existing rooms keep today's phone `share` toggle unchanged.

Wire list:
  {"v": 1, "seq", "room", "made", "mode", "hours": [[d, from, to]], "tz", "per": {tag: mode}, "jobs": {jobId: mode}}
  - mode in ("off", "photo", "1", "5", "15", "live")
  - d in 0..6 (Sunday = 0)
  - 0 <= from < to <= 1440 (minutes from midnight); empty hours means always
  - tag = sha256("biruni-pl-sk-1:" + secret)[:16] hex (so surveyor secrets are never published)

Envelope:
  {"l": canonical JSON, "ip": signer public key, "is": signature}
  Signed with ECDSA P-256 by `bridge.identity(key)` over `"BRPL1|" + hex(sha256(l))`.
  No trust-on-first-use: a link without a pin (`&i=`) refuses every policy.
"""
import datetime as dt
import hashlib
import json
import os
import re
import time

from . import recipients
from .recipients import _unb64u, canonical, next_seq, pin_of, sign_text, sign_with

FILE = "tracking_policy.json"
SIGN_PREFIX = "BRPL1|"
MODES = ("off", "photo", "15", "5", "1", "live")
RANK = {"off": 0, "photo": 1, "15": 2, "5": 3, "1": 4, "live": 5}
MAX_HOURS = 21
MAX_PER = 200
MAX_JOBS = 200
MAX_BYTES = 8000
TAG_RE = re.compile(r"^[0-9a-f]{16}$")
JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class PolicyError(ValueError):
    pass


def sk_tag(secret):
    """16-hex-char tag for a surveyor secret: `sha256("biruni-pl-sk-1:" + secret)[:16]`."""
    s = str(secret or "")
    if not s:
        return ""
    return hashlib.sha256(("biruni-pl-sk-1:" + s).encode("utf-8")).hexdigest()[:16]


def current_tz_minutes():
    """Office's current UTC offset in minutes (east of UTC positive, e.g. UTC+3 -> 180)."""
    off = dt.datetime.now().astimezone().utcoffset()
    if off is None:
        return 0
    return int(round(off.total_seconds() / 60.0))


def _clean_mode(m, allow_none=False):
    s = str(m or "").strip()
    if s in MODES:
        return s
    if allow_none:
        return None
    raise PolicyError(f"invalid tracking mode: {m!r}")


def _clean_hours(hours, strict=False):
    if hours is None:
        return []
    if not isinstance(hours, (list, tuple)):
        if strict:
            raise PolicyError("invalid hours")
        return []
    if strict and len(hours) > MAX_HOURS:
        raise PolicyError("at most 21 hour rows")
    out = []
    for row in hours:
        if (not isinstance(row, (list, tuple)) or len(row) != 3
                or any(isinstance(x, bool) or not isinstance(x, int) for x in row)):
            if strict:
                raise PolicyError("invalid hour row")
            continue
        d, f_min, t_min = int(row[0]), int(row[1]), int(row[2])
        if not (0 <= d <= 6 and 0 <= f_min < t_min <= 1440):
            if strict:
                raise PolicyError("invalid hour range")
            continue
        out.append([d, f_min, t_min])
        if len(out) >= MAX_HOURS:
            break
    return out


def _clean_tz(tz, default_now=True):
    if isinstance(tz, int) and not isinstance(tz, bool) and -840 <= tz <= 840:
        return int(tz)
    return current_tz_minutes() if default_now else 0


def load(folder):
    """Load `<cert folder>/tracking_policy.json`.

    Returns `{"seq": int, "mode": str, "hours": [[d, from, to]], "tz": int, "per": {id_or_tag: mode}, "jobs": {jobId: mode}}`.
    `seq == 0` means no policy has been saved by the office yet.
    """
    try:
        with open(os.path.join(folder, FILE), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    seq = d.get("seq") if isinstance(d.get("seq"), int) and not isinstance(d.get("seq"), bool) and d.get("seq") >= 0 else 0
    mode = d.get("mode") if d.get("mode") in MODES else "live"
    hours = _clean_hours(d.get("hours"), strict=False)
    tz = _clean_tz(d.get("tz"), default_now=True)
    raw_per = d.get("per") if isinstance(d.get("per"), dict) else (d.get("per_ids") if isinstance(d.get("per_ids"), dict) else {})
    per = {}
    for k, v in raw_per.items():
        ks = str(k or "").strip()[:64]
        if ks and v in MODES and len(per) < MAX_PER:
            per[ks] = v
    raw_jobs = d.get("jobs") if isinstance(d.get("jobs"), dict) else {}
    jobs = {}
    for k, v in raw_jobs.items():
        ks = str(k or "").strip()
        if JOB_RE.match(ks) and v in MODES and len(jobs) < MAX_JOBS:
            jobs[ks] = v
    return {"seq": seq, "mode": mode, "hours": hours, "tz": tz, "per": per, "jobs": jobs}


def save(folder, doc):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return doc


def set_policy(folder, mode, hours=(), tz=None, per_ids=None, jobs=None):
    """Validate and save a tracking policy, advancing `seq` with `recipients.next_seq`."""
    m = _clean_mode(mode)
    hrs = _clean_hours(list(hours or ()), strict=True)
    tz_val = current_tz_minutes() if tz is None else _clean_tz(tz, default_now=False)
    if tz is not None and (isinstance(tz, bool) or not isinstance(tz, int) or not (-840 <= tz <= 840)):
        raise PolicyError("invalid tz")
    per = {}
    if per_ids is not None:
        if not isinstance(per_ids, dict) or len(per_ids) > MAX_PER:
            raise PolicyError("invalid per_ids")
        for k, v in per_ids.items():
            if v in (None, ""):
                continue
            ks = str(k or "").strip()[:64]
            if not ks:
                raise PolicyError("invalid surveyor key in per_ids")
            per[ks] = _clean_mode(v)
    jmap = {}
    if jobs is not None:
        if not isinstance(jobs, dict) or len(jobs) > MAX_JOBS:
            raise PolicyError("invalid jobs")
        for k, v in jobs.items():
            if v in (None, ""):
                continue
            ks = str(k or "").strip()
            if not JOB_RE.match(ks):
                raise PolicyError(f"invalid job id: {k!r}")
            jmap[ks] = _clean_mode(v)
    cur = load(folder)
    doc = {
        "seq": next_seq(cur["seq"]),
        "mode": m,
        "hours": hrs,
        "tz": tz_val,
        "per": per,
        "jobs": jmap,
    }
    return save(folder, doc)


def merge(folder, doc):
    """Merge a policy from a key backup v2: the higher seq wins, and seq advances so phones never roll back."""
    cur = load(folder)
    if not isinstance(doc, dict) or not isinstance(doc.get("seq"), int) or isinstance(doc.get("seq"), bool):
        return cur
    if doc["seq"] <= 0 and cur["seq"] <= 0:
        return cur
    top = max(cur["seq"], doc["seq"])
    if doc["seq"] > cur["seq"]:
        save(folder, doc)
    d = load(folder)
    if d["seq"] or doc["seq"]:
        d["seq"] = next_seq(top)
        save(folder, d)
    return load(folder)


def _resolve_per_tags(per_map, surveyor_keys):
    """Convert `{surveyor_id_or_tag: mode}` into `{tag: mode}` using `surveyor_keys` without leaking secrets."""
    survs = {}
    if isinstance(surveyor_keys, dict):
        raw_s = surveyor_keys.get("surveyors") if isinstance(surveyor_keys.get("surveyors"), dict) else surveyor_keys
        if isinstance(raw_s, dict):
            survs = raw_s
    elif isinstance(surveyor_keys, (list, tuple)):
        for item in surveyor_keys:
            if isinstance(item, dict) and item.get("id"):
                survs[item["id"]] = item
    out = {}
    for k, mode in (per_map or {}).items():
        if mode not in MODES:
            continue
        ks = str(k or "").strip()
        if not ks:
            continue
        tag = ""
        if ks in survs and isinstance(survs[ks], dict):
            entry = survs[ks]
            if entry.get("revoked"):
                continue
            sec = str(entry.get("secret") or "")
            if sec:
                tag = sk_tag(sec)
        elif TAG_RE.match(ks):
            tag = ks
        else:
            # Check if ks matches a surveyor secret directly (caller passed secret instead of id)
            for s in survs.values():
                if isinstance(s, dict) and not s.get("revoked") and s.get("secret") == ks:
                    tag = sk_tag(ks)
                    break
        if tag and TAG_RE.match(tag) and len(out) < MAX_PER:
            out[tag] = mode
    return out


def build(folder, room, surveyor_keys=None, tz=None):
    """Build the policy wire dict for `room`. Recomputes `tz` from the office's current offset unless given."""
    d = load(folder)
    tz_val = current_tz_minutes() if tz is None else _clean_tz(tz, default_now=True)
    per_tags = _resolve_per_tags(d.get("per") or {}, surveyor_keys)
    return {
        "v": 1,
        "seq": d["seq"],
        "room": str(room or ""),
        "made": int(time.time()),
        "mode": d["mode"],
        "hours": d["hours"][:MAX_HOURS],
        "tz": tz_val,
        "per": per_tags,
        "jobs": dict(d.get("jobs") or {}),
    }


def signed(folder, key, surveyor_keys=None):
    """Return `{"l": canonical JSON, "ip", "is"}` signed by `bridge.identity(key)`, or `None` when `seq < 1`."""
    from . import bridge

    if not key or load(folder)["seq"] < 1:
        return None
    l_text = canonical(build(folder, key[:12], surveyor_keys=surveyor_keys))
    ip, sig = sign_with(bridge.identity(key)[0], l_text, prefix=SIGN_PREFIX)
    return {"l": l_text, "ip": ip, "is": sig}


def verify(pl, room, pin, last_seq=0):
    """Phone-side checks in Python. No TOFU: a missing pin refuses every policy."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    if not pin or not isinstance(pin, str):
        raise PolicyError("no pin")
    if not isinstance(pl, dict) or not all(isinstance(pl.get(x), str) for x in ("l", "ip", "is")):
        raise PolicyError("not a policy")
    if len(pl["l"].encode("utf-8")) > MAX_BYTES:
        raise PolicyError("policy too large")
    try:
        if pin_of(pl["ip"]) != pin:
            raise PolicyError("wrong signer")
    except (ValueError, TypeError) as e:
        raise PolicyError("wrong signer") from e
    try:
        raw, sig = _unb64u(pl["ip"]), _unb64u(pl["is"])
        if len(raw) != 65 or len(sig) != 64:
            raise ValueError
        pk = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        pk.verify(der, sign_text(pl["l"], prefix=SIGN_PREFIX).encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, TypeError) as e:
        raise PolicyError("bad signature") from e
    try:
        lst = json.loads(pl["l"])
    except ValueError as e:
        raise PolicyError("not a policy") from e
    if (not isinstance(lst, dict) or lst.get("v") != 1
            or not isinstance(lst.get("seq"), int) or isinstance(lst.get("seq"), bool) or lst["seq"] < 1):
        raise PolicyError("not a policy")
    if lst.get("room") != room:
        raise PolicyError("another room")
    if lst["seq"] < int(last_seq or 0):
        raise PolicyError("rollback")
    if lst.get("mode") not in MODES:
        raise PolicyError("invalid mode")
    hrs = lst.get("hours")
    if not isinstance(hrs, list) or len(hrs) > MAX_HOURS:
        raise PolicyError("invalid hours")
    _clean_hours(hrs, strict=True)
    tz = lst.get("tz")
    if not isinstance(tz, int) or isinstance(tz, bool) or not (-840 <= tz <= 840):
        raise PolicyError("invalid tz")
    per = lst.get("per")
    if not isinstance(per, dict) or len(per) > MAX_PER:
        raise PolicyError("invalid per")
    for k, v in per.items():
        if not isinstance(k, str) or not TAG_RE.match(k) or v not in MODES:
            raise PolicyError("invalid per entry")
    jobs = lst.get("jobs")
    if not isinstance(jobs, dict) or len(jobs) > MAX_JOBS:
        raise PolicyError("invalid jobs")
    for k, v in jobs.items():
        if not isinstance(k, str) or not JOB_RE.match(k) or v not in MODES:
            raise PolicyError("invalid job entry")
    return lst


def in_hours(hours, tz_min, now_ms=None):
    """True when `hours` is empty or `now_ms` (shifted by `tz_min`) falls inside at least one `[d, from, to]` row."""
    if not hours:
        return True
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    tz_ms = int(tz_min or 0) * 60_000
    shifted_s = (int(now_ms) + tz_ms) // 1000
    tm = time.gmtime(shifted_s)
    # Python tm_wday: Monday=0..Sunday=6 -> convert to Sunday=0..Saturday=6
    day = (tm.tm_wday + 1) % 7
    minute = tm.tm_hour * 60 + tm.tm_min
    for row in hours:
        if isinstance(row, (list, tuple)) and len(row) == 3:
            d, f_min, t_min = row[0], row[1], row[2]
            if d == day and f_min <= minute < t_min:
                return True
    return False


def base_mode(lst, tag="", job=""):
    """Mode chosen by `per[tag] ?? jobs[job] ?? mode` before work-hours or consent checks."""
    if not isinstance(lst, dict):
        return "off"
    per = lst.get("per") if isinstance(lst.get("per"), dict) else {}
    jobs = lst.get("jobs") if isinstance(lst.get("jobs"), dict) else {}
    if tag and tag in per and per[tag] in MODES:
        return per[tag]
    if job and job in jobs and jobs[job] in MODES:
        return jobs[job]
    m = lst.get("mode")
    return m if m in MODES else "off"


def effective(lst, tag="", job="", now_ms=None):
    """Effective tracking mode at `now_ms`: `per[tag] ?? jobs[job] ?? mode`, falling to `'off'` outside work hours."""
    m = base_mode(lst, tag=tag, job=job)
    if m == "off":
        return "off"
    if not in_hours(lst.get("hours") or (), lst.get("tz", 0), now_ms=now_ms):
        return "off"
    return m
