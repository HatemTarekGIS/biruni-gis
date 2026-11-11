"""Seals: every number a dashboard sends carries a short code (SPEC.md §4).

The PC keeps a log (SQLite in the settings folder): code -> the statement it stands for (which dashboard,
widget, value as sent, filters, card, time, and a digest of the data the number was computed from) and the
statement's ed25519 signature with this PC's dashboard key. A screenshot sent around can be checked: the code
gives the statement, the signature shows this PC made it, and with the data at hand the value is computed
again (recheck).
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time

from ..secure import crypto
from . import cards

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
BIG = 2000                     # a value whose JSON is longer than this is kept as its sha256
_PATH = []                     # tests: another log file
_LOCK = threading.Lock()
_CON = {}


def log_path():
    if _PATH:
        return _PATH[0]
    from qgis.core import QgsApplication

    folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "dashboard_seals.sqlite")


def _con():
    p = log_path()
    con = _CON.get(p)
    if con is None:
        con = sqlite3.connect(p, check_same_thread=False, timeout=20)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS seals (code TEXT PRIMARY KEY, h TEXT UNIQUE, ts REAL, dash TEXT, "
                    "widget TEXT, card TEXT, statement TEXT, sig TEXT)")
        _CON.clear()
        _CON[p] = con
    return con


def new_code():
    n = secrets.randbits(40)
    raw = "".join(ALPHABET[(n >> (5 * i)) & 31] for i in range(8))
    return raw[:4] + "-" + raw[4:]


def normalize(code):
    s = "".join({"O": "0", "I": "1", "L": "1", "U": "V"}.get(c, c) for c in (code or "").upper() if c.isalnum())
    return s[:4] + "-" + s[4:] if len(s) == 8 and all(c in ALPHABET for c in s) else None


def value_of(data):
    """A widget's data as kept in a statement: itself, or its sha256 when big (a map)."""
    d = {k: v for k, v in (data or {}).items() if k not in ("seal", "prev", "delta")}  # prev/delta: of the moment
    text = crypto.canonical(d)
    if len(text) > BIG:
        return {"sha256": hashlib.sha256(text).hexdigest()}
    return d


def make(dash, widget, title, data, filters, card, digest):
    """The seal code of one number as sent (the same statement gives the same code). card: the payload or None."""
    scope = None
    if card:
        scope = {"id": card.get("id"), "name": card.get("name"), "zone": card.get("zone"),
                 "filters": card.get("filters")}
    st = {"v": 1, "dash": dash, "widget": widget, "title": title, "value": value_of(data),
          "filters": filters or {}, "card": scope, "data": digest}
    h = hashlib.sha256(crypto.canonical(st)).hexdigest()
    with _LOCK:
        con = _con()
        row = con.execute("SELECT code FROM seals WHERE h=?", (h,)).fetchone()
        if row:
            return row[0]
        st["ts"] = round(time.time(), 3)
        sig = crypto.sign(cards.seed(), crypto.canonical(st)).hex()
        for _ in range(5):
            code = new_code()
            try:
                con.execute("INSERT INTO seals (code, h, ts, dash, widget, card, statement, sig) "
                            "VALUES (?,?,?,?,?,?,?,?)", (code, h, st["ts"], dash, widget, (scope or {}).get("id"),
                                                         json.dumps(st, ensure_ascii=False), sig))
                con.commit()
                return code
            except sqlite3.IntegrityError:
                continue
    raise RuntimeError("no seal code")


def lookup(code):
    """{"code", "statement", "sig"} of a code, or None."""
    c = normalize(code)
    if not c:
        return None
    with _LOCK:
        row = _con().execute("SELECT statement, sig FROM seals WHERE code=?", (c,)).fetchone()
    if not row:
        return None
    return {"code": c, "statement": json.loads(row[0]), "sig": row[1]}


def verify(statement, sig, pub=None):
    """True when ``sig`` (hex) is this PC's (or ``pub``'s, hex) signature of the statement."""
    try:
        pk = bytes.fromhex(pub) if pub else bytes.fromhex(cards.public_hex())
        return crypto.verify(pk, bytes.fromhex(sig), crypto.canonical(statement))
    except (ValueError, TypeError):
        return False


def as_text(code):
    """A seal as one line of text (for a QR or to paste into another Biruni): BDS1.<statement>.<sig>."""
    s = lookup(code)
    if not s:
        return None
    return "BDS1." + cards.b64u(crypto.canonical(s["statement"])) + "." + s["sig"]


def from_text(text):
    """(statement, sig) of a pasted seal text, or None."""
    try:
        head, body, sig = (text or "").strip().split(".")
        if head != "BDS1":
            return None
        return json.loads(cards.unb64u(body).decode("utf-8")), sig
    except (ValueError, UnicodeDecodeError):
        return None


def recheck(statement, project=None):
    """Compute the statement's number again from the data now on this PC.
    -> {"same": bool, "value": now, "data_same": bool} or {"error": why}."""
    from . import data, model

    d = model.get(project, statement.get("dash")) if statement.get("dash") else None
    if d is None:
        return {"error": "the dashboard is not in this project"}
    w = next((x for x in d.get("widgets") or [] if x.get("id") == statement.get("widget")), None)
    if w is None:
        return {"error": "the widget is not in the dashboard any more"}
    scope = statement.get("card") or None
    card = None
    if scope:
        card = {"id": scope.get("id"), "zone": scope.get("zone"), "filters": scope.get("filters"), "widgets": "*",
                "hide": []}
    comp = data.Computer(d)
    res = comp.compute(card=card, filters=statement.get("filters") or {}, only=[w["id"]])
    got = res["widgets"].get(w["id"])
    if got is None:
        return {"error": "the number cannot be computed here"}
    now = value_of(got)
    return {"same": now == statement.get("value"), "value": now,
            "data_same": res["digests"].get(w.get("source")) == statement.get("data")}
