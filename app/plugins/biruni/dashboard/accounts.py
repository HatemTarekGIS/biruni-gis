"""Accounts of a dashboard: who may open it from a printed live paper's QR code (or any link) without a card.

* A visitor: anyone who scans the paper. The dashboard's visitor template decides what a visitor sees (default:
  the map only, coloured by its style field, with no labels, no popup fields and no numbers). Off by default
  for a dashboard until the owner turns it on in "Accounts…".
* A user: a username and a password set on this PC. Each user has a template of their own: widgets, hidden
  fields, a zone (polygon, EPSG:4326 WKT), forced filters, how long a sign-in lasts, and whether the account
  is turned off.

Passwords are never kept: only scrypt(password, a salt of the user's own) — compared in constant time. After
5 wrong passwords for a username in 5 minutes the username is locked for 5 minutes; a session may try at most
once a second and 20 times in all.

A good sign-in (or a visitor) gets a card (cards.py) signed by this PC like any other card, short-lived, that is
not added to the project's list of issued cards (visitors would fill it). Signing out revokes that card.
Stored in the project's own entries (like the papers), per dashboard: {dash: {"users": {...}, "visitor": {...}}}.
"""
import hashlib
import hmac
import json
import secrets
import time

from qgis.core import QgsGeometry, QgsProject

from ..secure import crypto
from . import cards, model

ENTRY = "accounts"
LOCK_TRIES = 5
LOCK_WINDOW = 300
LOCK_FOR = 300
SESSION_MAX = 20
VISITOR_HOURS = 6
USER_HOURS = 12
_SCRYPT = dict(n=1 << 14, r=8, p=1, maxmem=64 << 20, dklen=32)
_FAILS = {}          # (dash, username) -> [times]
_LOCKED = {}         # (dash, username) -> until


class AccountError(Exception):
    """why: no visitors | bad login | locked | slow down | disabled | no dashboard"""

    def __init__(self, why, wait=0):
        super().__init__(why)
        self.why = why
        self.wait = int(wait)


def _project(project=None):
    return project or QgsProject.instance()


def norm_user(name):
    return " ".join(str(name or "").strip().lower().split())[:64]


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(str(password).encode("utf-8"), salt=salt, **_SCRYPT)
    return salt.hex(), h.hex()


def check_password(password, salt_hex, hash_hex):
    try:
        _s, h = hash_password(password, bytes.fromhex(salt_hex))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(h, str(hash_hex))


# ---- storage -----------------------------------------------------------------------------------------------------
def load(dash_id, project=None):
    allv = model._entries(_project(project), ENTRY)
    v = allv.get(dash_id) or {}
    return {"users": dict(v.get("users") or {}), "visitor": dict(v.get("visitor") or {"on": False})}


def save(dash_id, value, project=None):
    project = _project(project)
    allv = model._entries(project, ENTRY)
    allv[dash_id] = {"users": value.get("users") or {}, "visitor": value.get("visitor") or {"on": False}}
    model._put_entries(project, ENTRY, allv)


def _template(widgets="*", hide=(), zone=None, filters=None, hours=USER_HOURS):
    if zone:
        g = QgsGeometry.fromWkt(zone)
        if g.isNull() or g.isEmpty():
            raise ValueError("the zone is not a geometry")
        zone = g.asWkt(7)
    return {"widgets": widgets if widgets == "*" else [str(w) for w in widgets],
            "hide": sorted({str(h) for h in hide or ()}), "zone": zone or None, "filters": filters or None,
            "hours": max(0.25, min(24 * 30, float(hours or USER_HOURS)))}


def set_user(dash_id, username, password=None, name="", widgets="*", hide=(), zone=None, filters=None,
             hours=USER_HOURS, disabled=False, project=None):
    """Add or change a user (password None keeps the old one; a new user needs one)."""
    u = norm_user(username)
    if not u:
        raise ValueError("a username is needed")
    acc = load(dash_id, project)
    old = acc["users"].get(u) or {}
    rec = dict(old)
    rec.update(_template(widgets, hide, zone, filters, hours))
    rec.update({"user": u, "name": str(name or username)[:80], "disabled": bool(disabled)})
    if password is not None:
        if len(str(password)) < 4:
            raise ValueError("the password is too short (4 characters at least)")
        rec["salt"], rec["hash"] = hash_password(password)
    elif not old.get("hash"):
        raise ValueError("a new user needs a password")
    acc["users"][u] = rec
    save(dash_id, acc, project)
    return rec


def remove_user(dash_id, username, project=None):
    acc = load(dash_id, project)
    acc["users"].pop(norm_user(username), None)
    save(dash_id, acc, project)


def set_visitor(dash_id, on=True, widgets=None, hide=None, zone=None, filters=None, hours=VISITOR_HOURS, project=None,
                definition=None):
    """The visitor template. widgets/hide None = the default (the maps only, coloured, nothing else)."""
    acc = load(dash_id, project)
    d = definition or _def(dash_id, project)
    if widgets is None:
        widgets = [w["id"] for w in (d or {}).get("widgets") or [] if w.get("type") in ("map", "choropleth")]
    if hide is None:
        hide = visitor_hide(d, project)
    rec = _template(widgets, hide, zone, filters, hours)
    rec["on"] = bool(on)
    acc["visitor"] = rec
    save(dash_id, acc, project)
    return rec


def _def(dash_id, project=None):
    try:
        return model.get(_project(project), dash_id)
    except Exception:  # noqa: BLE001
        return None


def _layer_fields(layer_id, project):
    lyr = _project(project).mapLayer(layer_id)
    try:
        return [f.name() for f in lyr.fields()]
    except AttributeError:
        return []


def visitor_hide(d, project=None):
    """Every field of the dashboard's sources except those that colour its maps (style.field): no labels, no popup
    fields, no numbers by other fields reach a visitor."""
    if not d:
        return []
    keep, out = set(), set()
    for w in d.get("widgets") or []:
        o = w.get("options") or {}
        for lay in o.get("layers") or []:
            f = (lay.get("style") or {}).get("field")
            if f:
                keep.add(f)
        f = (w.get("style") or {}).get("by") or (w.get("style") or {}).get("field")
        if f:
            keep.add(f)
    for src in (d.get("sources") or {}).values():
        out.update(_layer_fields(src.get("layer"), project))
    return sorted(out - keep)


# ---- cards for visitors and users --------------------------------------------------------------------------------
def _card(dash_id, name, t, exp, kind):
    """A signed card from template t, NOT added to the project's list of issued cards."""
    now = time.time()
    payload = {"v": 1, "id": model.new_id("c"), "dash": dash_id, "name": str(name or "")[:80],
               "widgets": t.get("widgets", "*"), "hide": list(t.get("hide") or []), "zone": t.get("zone"),
               "filters": t.get("filters"), "input": False, "iat": round(now, 3), "exp": round(float(exp), 3),
               "acct": kind}
    body = crypto.canonical(payload)
    sig = crypto.sign(cards.seed(), body)
    return f"{cards.PREFIX}.{cards.b64u(body)}.{cards.b64u(sig)}", payload


def visitor_card(dash_id, project=None):
    acc = load(dash_id, project)
    v = acc["visitor"]
    if not v.get("on"):
        raise AccountError("no visitors")
    exp = time.time() + float(v.get("hours") or VISITOR_HOURS) * 3600
    return _card(dash_id, "Visitor", v, exp, "visitor")


def _locked(key, now):
    until = _LOCKED.get(key, 0)
    if until > now:
        return until - now
    _LOCKED.pop(key, None)
    return 0


def login(dash_id, username, password, project=None, now=None, session=None):
    """(card text, payload) for a good username + password; AccountError otherwise. session: any object; it
    gets a counter so one connection cannot try forever."""
    now = time.time() if now is None else now
    if session is not None:
        tries = getattr(session, "_acct_tries", [])
        if tries and now - tries[-1] < 1.0:
            raise AccountError("slow down", 1)
        if len(tries) >= SESSION_MAX:
            raise AccountError("locked", LOCK_FOR)
        session._acct_tries = tries + [now]
    u = norm_user(username)
    key = (dash_id, u)
    wait = _locked(key, now)
    if wait:
        raise AccountError("locked", wait)
    rec = load(dash_id, project)["users"].get(u)
    ok = bool(rec) and check_password(password, rec.get("salt", ""), rec.get("hash", ""))
    if not rec:                      # the same work for an unknown name: the time does not tell which names exist
        hash_password(password, b"\0" * 16)
    if not ok:
        fails = [t for t in _FAILS.get(key, []) if now - t < LOCK_WINDOW] + [now]
        _FAILS[key] = fails
        if len(fails) >= LOCK_TRIES:
            _LOCKED[key] = now + LOCK_FOR
            _FAILS.pop(key, None)
            raise AccountError("locked", LOCK_FOR)
        raise AccountError("bad login")
    if rec.get("disabled"):
        raise AccountError("disabled")
    _FAILS.pop(key, None)
    exp = now + float(rec.get("hours") or USER_HOURS) * 3600
    return _card(dash_id, rec.get("name") or u, rec, exp, "user:" + u)


def logout(card_payload, project=None):
    if card_payload and card_payload.get("id"):
        try:
            cards.revoke(card_payload["id"], card_payload.get("dash", ""), project)
        except Exception:  # noqa: BLE001
            pass


# ---- stable addresses for printed papers ---------------------------------------------------------------------------
_STABLE_KEYS = {"lan": "biruni/dashboard/paper-lan-token", "bridge": "biruni/dashboard/paper-bridge-key"}


def stable(kind, make):
    """A value kept (encrypted) for this Windows user, so the addresses printed on papers keep working when the
    sharing is started again. kind: lan | bridge; make(): a new value."""
    from qgis.core import QgsSettings

    from ..field_photos.tunnel import protect, unprotect

    s = QgsSettings()
    try:
        v = unprotect(s.value(_STABLE_KEYS[kind], "") or "") or ""
    except Exception:  # noqa: BLE001
        v = ""
    if not v:
        v = make()
        s.setValue(_STABLE_KEYS[kind], protect(v))
    return v


def reset_stable():
    """A new paper address: forget the kept LAN token, bridge key and the PC identity that goes with the key. Papers
    printed before stop reaching live data (their QR codes carry the old ones); the next sharing makes new ones.
    -> the kinds that were reset"""
    from qgis.core import QgsSettings

    from ..field_photos.tunnel import unprotect

    s = QgsSettings()
    done = []
    for kind, name in _STABLE_KEYS.items():
        v = ""
        try:
            v = unprotect(s.value(name, "") or "") or ""
        except Exception:  # noqa: BLE001
            pass
        if kind == "bridge" and v:
            from ..field_photos.bridge import forget_identity

            forget_identity(v)
        if s.contains(name):
            s.remove(name)
            done.append(kind)
    return done


def summary(dash_id, project=None):
    acc = load(dash_id, project)
    return {"visitor": bool(acc["visitor"].get("on")),
            "users": [{k: u.get(k) for k in ("user", "name", "disabled", "hours")} for u in acc["users"].values()]}


def dumps(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True)
