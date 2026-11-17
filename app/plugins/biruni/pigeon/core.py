"""Making, opening and carrying pigeon packs (delay-tolerant sync of a team room, with no network at all).

A pack is one sealed, signed message of the room:

* "up"   - made by a member: its changes not yet in the room's stream (its outbox, each change signed by its
           author as over the network) and how far it has the stream ("since").
* "down" - made by the host: a run of the room's stream (entries after ``after`` up to ``hi``), how far each
           member has it (``seen``, from the packs the host received), the host's answers to the members' changes
           (refused, conflict, held) and the room's roster.

A file (or a run of codes on the screen) is an envelope of packs: the maker's own and those it carries for
others. Opening one, each computer takes what is for it and keeps the rest to carry on:

* the host decides each change of an up pack exactly as if it came over the network (``Hub.apply_remote``): the
  change must carry its author's signature, the author must still be a member with the right to edit, the
  delete guard and the work areas apply. A token is never carried, so a pack gives no one a way in.
* a member applies a down pack's entries only when they continue its own place in the stream (``after <= since``);
  a pack that starts later waits, carried, until the packs before it arrive. Duplicates do nothing (by uid).
* carried packs are dropped once their changes are in the stream (up), once every member has them (down), when a
  newer up pack of the same member arrives (it holds the same changes), or when they expire.

Every pack is sealed with the room key (AES-256-GCM, its own AAD) and signed with its maker's identity key; the
signature is checked against the roster. Packs may be days old: they expire after EXPIRY instead of the ten
minutes of a network message.
"""
import json
import os
import re
import time
import uuid

from ..secure import crypto

MAGIC = b"BIRUNI-PIGEON-1\n"
AAD = b"biruni-pigeon-1"
EXT = ".bpigeon"
EXPIRY = 30 * 86400               # seconds a pack is valid
MAX_PACK = 4 << 20                # bytes of changes in one pack (a larger outbox or stream run is split)
MAX_FILE = 24 << 20               # bytes of one envelope (more packs go into further files)
MAX_OPEN = 64 << 20               # bytes a pack may grow to once decompressed
MAX_CARRY = 300                   # packs carried for others at most (the oldest go first)
DOWN_KEEP = 20000                 # stream entries at most in the host's packs for a member it knows nothing of
RESULTS_KEEP = 500                # answers kept per member

SCHEMA = """
CREATE TABLE IF NOT EXISTS pigeon_carry (id TEXT PRIMARY KEY, kind TEXT, maker TEXT, made REAL, expires REAL,
    after INTEGER, hi INTEGER, uids TEXT, blob BLOB, waiting INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pigeon_seen (id TEXT PRIMARY KEY, ts REAL);
"""


class PigeonError(Exception):
    """Not a pack of this room, or this computer cannot open it now."""


# ---- the room store's pigeon tables (added beside the room's own; nothing of the room changes) ------------------
def _ready(store):
    if not getattr(store, "_pigeon_ready", False):
        with store._lock:
            store.con.executescript(SCHEMA)
        store._pigeon_ready = True
    return store


def _q(store, sql, args=()):
    with store._lock:
        return store.con.execute(sql, args).fetchall()


def _x(store, sql, args=()):
    with store._lock:
        store.con.execute(sql, args)


def _seen(store, pid):
    return bool(_q(store, "SELECT 1 FROM pigeon_seen WHERE id=?", (pid,)))


def _mark_seen(store, pid):
    _x(store, "INSERT OR REPLACE INTO pigeon_seen (id, ts) VALUES (?, ?)", (pid, time.time()))


def _carried(store, pid):
    return bool(_q(store, "SELECT 1 FROM pigeon_carry WHERE id=?", (pid,)))


def _carry(store, body, blob, waiting=False):
    if body["kind"] == "up":   # a newer pack of the same member holds the same changes (and any new ones)
        _x(store, "DELETE FROM pigeon_carry WHERE kind='up' AND maker=? AND made<?", (body["maker"], body["made"]))
        if _q(store, "SELECT 1 FROM pigeon_carry WHERE kind='up' AND maker=? AND made>=?",
              (body["maker"], body["made"])):
            return
    uids = [o.get("uid") for o in body.get("ops") or []]
    _x(store, "INSERT OR REPLACE INTO pigeon_carry (id, kind, maker, made, expires, after, hi, uids, blob, waiting) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
       (body["id"], body["kind"], body["maker"], body["made"], body["expires"], body.get("after"), body.get("hi"),
        json.dumps(uids), bytes(blob), 1 if waiting else 0))


def counts(node):
    """What this computer has for the others: {"outbox", "carried", "waiting", "role"}."""
    st = _ready(node.store)
    now = time.time()
    carried = _q(st, "SELECT COUNT(*) FROM pigeon_carry WHERE expires>=? AND waiting=0", (now,))[0][0]
    waiting = _q(st, "SELECT COUNT(*) FROM pigeon_carry WHERE expires>=? AND waiting=1", (now,))[0][0]
    return {"outbox": st.outbox_count() if _role(node) == "member" else 0, "carried": carried, "waiting": waiting,
            "role": _role(node)}


def _role(node):
    return "host" if (node.role == "host" or node.store.get("role") == "host") else "member"


# ---- sealing and signing ---------------------------------------------------------------------------------------
def _sign(node, body):
    body = dict(body, pub=node.pub)
    body["sig"] = crypto.sign(bytes.fromhex(node.ident["seed"]), crypto.canonical(body)).hex()
    return body


def _signed_ok(body):
    b = {k: v for k, v in body.items() if k != "sig"}
    try:
        return crypto.verify(bytes.fromhex(b.get("pub") or ""), bytes.fromhex(body.get("sig") or ""),
                             crypto.canonical(b))
    except (ValueError, TypeError):
        return False


def seal(node, body):
    return crypto.seal(node.keys.key, crypto.canonical(_sign(node, body)), AAD, compress=True)


def unseal(node, blob):
    try:
        body = json.loads(crypto.unseal(node.keys.key, blob, AAD, max_size=MAX_OPEN).decode("utf-8"))
    except (crypto.CryptoError, ValueError, UnicodeDecodeError):
        raise PigeonError("sealed with another key or altered") from None
    if not isinstance(body, dict) or body.get("v") != 1 or body.get("kind") not in ("up", "down"):
        raise PigeonError("not a pack")
    if not _signed_ok(body):
        raise PigeonError("bad signature")
    return body


def _base(node, kind):
    now = time.time()
    return {"v": 1, "id": uuid.uuid4().hex, "kind": kind, "room": node.keys.tag, "sid": node.store.get("sid"),
            "epoch": node.epoch, "maker": node.me_id, "name": node.ident.get("name", ""), "made": now,
            "expires": now + EXPIRY}


def _parts(items, size=MAX_PACK):
    part, total = [], 0
    for it in items:
        n = len(json.dumps(it))
        if part and total + n > size:
            yield part
            part, total = [], 0
        part.append(it)
        total += n
    yield part      # an empty part too: a pack without changes still says where its maker is


# ---- making ----------------------------------------------------------------------------------------------------
def _up_packs(node):
    held = set(node.store.get("pigeon_held") or [])
    ops = [o for o in node.store.outbox(1 << 30) if o.get("uid") not in held]
    out = []
    for part in _parts(ops):
        b = _base(node, "up")
        b.update(ops=part, since=node.since)
        out.append(b)
    return out


def _down_packs(node, low=None):
    st = node.store
    head = st.head()
    hub = node.hub
    roster = hub.roster() if hub is not None else node.roster
    seen = st.get("pigeon_seen_map") or {}
    if low is None:
        members = [m["id"] for m in roster if not m.get("host") and m.get("state") != "out" and m["id"] != node.me_id]
        known = [int(seen[m]) for m in members if m in seen]
        low = min(known) if known and len(known) == len(members) else 0
    low = max(int(low), head - DOWN_KEEP, 0)
    entries, at = [], low
    while at < head:
        rows = st.since(at, 2000)
        if not rows:
            break
        entries.extend(rows)
        at = rows[-1]["seq"]
    results = st.get("pigeon_results") or {}
    refused = dict(hub.refused_for) if hub is not None else {}
    from ..team.hub import room_settings
    settings = hub.settings() if hub is not None else room_settings(st)
    out, after = [], low
    for part in _parts(entries):
        b = _base(node, "down")
        hi = part[-1]["seq"] if part else head
        b.update(ops=part, after=after, hi=hi, head=head, seen=seen, results=results, refused=refused,
                 roster=roster, settings=settings)
        out.append(b)
        after = hi
    return out


def envelopes(blobs, room=None):
    """Split sealed pack blobs into envelope byte strings, each at most MAX_FILE bytes."""
    files, cur, size = [], [], 0
    for blob in blobs:
        n = len(blob) * 4 // 3 + 4
        if cur and size + n > MAX_FILE:
            files.append(cur)
            cur, size = [], 0
        cur.append(blob)
        size += n
    if cur:
        files.append(cur)
    head = {"room": room} if room is not None else {}
    return [MAGIC + crypto.canonical(dict(head, packs=[crypto.b64(b) for b in f])) for f in files]


def make(node):
    """The envelopes (bytes) this computer gives: its own packs and the ones it carries. More than one when large."""
    st = _ready(node.store)
    prune(node)
    if _role(node) != "host":
        node.store.put("pigeon_only", True)  # packs by hand: a lost network must not make this computer the host
    own = _down_packs(node) if _role(node) == "host" else _up_packs(node)
    blobs = [seal(node, b) for b in own]
    now = time.time()
    blobs += [bytes(r[0]) for r in _q(st, "SELECT blob FROM pigeon_carry WHERE expires>=? ORDER BY made", (now,))]
    return envelopes(blobs, node.keys.tag)


def file_name(node):
    name = node.store.get("room_name", "") or "room"
    who = node.ident.get("name", "") or "me"
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", f"{name}-{who}").strip("-")[:60] or "room"
    return f"{safe}-{time.strftime('%Y%m%d-%H%M')}{EXT}"


def save(node, folder):
    """Write this computer's envelopes into folder. -> [paths]"""
    paths = []
    datas = make(node)
    base = file_name(node)[:-len(EXT)]
    for i, data in enumerate(datas):
        p = os.path.join(folder, base + (f"-{i + 1}" if len(datas) > 1 else "") + EXT)
        with open(p, "wb") as f:
            f.write(data)
        paths.append(p)
    return paths


# ---- opening ---------------------------------------------------------------------------------------------------
def _envelope(data):
    data = bytes(data or b"")
    if not data.startswith(MAGIC):
        raise PigeonError("not a pigeon pack")
    try:
        env = json.loads(data[len(MAGIC):].decode("utf-8"))
        return env.get("room"), [crypto.unb64(b) for b in env.get("packs") or []]
    except (ValueError, UnicodeDecodeError, AttributeError, TypeError):
        raise PigeonError("damaged pack") from None


def _roster_entry(node, mid):
    roster = node.hub.roster() if node.hub is not None else (node.roster or node.store.get("roster") or [])
    return next((m for m in roster if m.get("id") == mid), None)


def open_data(node, data):
    """Take what an envelope brings. -> report {"packs", "in", "applied", "carried", "waiting", "dups", "refused",
    "conflicts", "results"}"""
    st = _ready(node.store)
    room, blobs = _envelope(data)
    if room != node.keys.tag:
        raise PigeonError("another room")
    rep = {"packs": len(blobs), "in": 0, "applied": 0, "carried": 0, "waiting": 0, "dups": 0, "refused": [],
           "conflicts": 0, "decided": {}}
    host = _role(node) == "host"
    if not host:
        node.store.put("pigeon_only", True)  # packs by hand: a lost network must not make this computer the host
    now = time.time()
    for blob in blobs:
        try:
            body = unseal(node, blob)
        except PigeonError as e:
            rep["refused"].append(str(e))
            continue
        pid = str(body.get("id", ""))
        if body.get("room") != node.keys.tag:
            rep["refused"].append("another room")
            continue
        if float(body.get("expires", 0)) < now:
            rep["refused"].append("expired")
            continue
        if body["maker"] == node.me_id or _seen(st, pid) or _carried(st, pid):
            rep["dups"] += 1
            continue
        who = _roster_entry(node, body["maker"])
        if who is not None and who.get("pub") and who["pub"] != body.get("pub"):
            rep["refused"].append("signed by someone else")
            continue
        if host:
            if body["kind"] == "down":
                rep["refused"].append("another host's stream" if body.get("sid") != st.get("sid") else "a host pack")
                continue
            if who is None or who.get("state") == "out" or not who.get("pub"):
                rep["refused"].append("not a member")
                continue
            _host_take(node, body, rep)
            _mark_seen(st, pid)
            continue
        if body["kind"] == "up":            # a member's changes: carried on towards the host
            _carry(st, body, blob)
            _mark_seen(st, pid)
            rep["carried"] += 1
            continue
        if body.get("sid") != st.get("sid"):
            rep["refused"].append("another host's stream")
            continue
        if who is None or not who.get("host") or not who.get("pub"):
            rep["refused"].append("not the room's host")
            continue
        r = _member_take(node, body, rep)
        _carry(st, body, blob, waiting=(r == "waiting"))
        if r != "waiting":
            _mark_seen(st, pid)
        rep["carried"] += 1
    if not host:
        _retry_waiting(node, rep)
    prune(node)
    rep["waiting"] = _q(st, "SELECT COUNT(*) FROM pigeon_carry WHERE waiting=1")[0][0]
    return rep


def open_file(node, path):
    with open(path, "rb") as f:
        return open_data(node, f.read())


def _host_take(node, body, rep):
    hub = node.hub
    if hub is None:
        raise PigeonError("the room is not open on this computer")
    st = node.store
    maker = body["maker"]
    seen = st.get("pigeon_seen_map") or {}
    seen[maker] = max(int(seen.get(maker, 0)), int(body.get("since") or 0))
    st.put("pigeon_seen_map", seen)
    results = st.get("pigeon_results") or {}
    mine = results.setdefault(maker, {})
    for op in body.get("ops") or []:
        op = dict(op)
        if op.get("author") != maker:      # a member's pack carries only its own changes
            rep["refused"].append("a change of someone else")
            continue
        if st.has(str(op.get("uid", ""))):
            rep["dups"] += 1
            continue
        member = hub._member(maker)
        r = hub.apply_remote(op, member)
        if r == "later":                   # the host's own unsaved edit of that feature: saved, then decided
            node.commit_layer(op.get("layer"))
            r = hub.apply_remote(op, member, force=True)
        if not isinstance(r, dict):
            continue
        rep["in"] += 1
        status = r.get("status")
        rep["decided"][status] = rep["decided"].get(status, 0) + 1
        if r.get("conflicts"):
            rep["conflicts"] += 1
        if status in ("refused", "held", "conflict") or r.get("conflicts"):
            mine[r.get("uid") or op["uid"]] = {k: r.get(k) for k in ("uid", "status", "why", "conflicts", "layer",
                                                                        "fid", "mine", "theirs", "other")}
        if status == "refused":
            rep["refused"].append(str(r.get("why") or "refused"))
    for k in list(mine)[:-RESULTS_KEEP]:
        del mine[k]
    st.put("pigeon_results", results)


def _member_take(node, body, rep):
    st = node.store
    if float(body.get("made", 0)) > float(st.get("pigeon_down_made") or 0):   # the newest host pack says these
        st.put("pigeon_down_made", float(body["made"]))
        if body.get("roster"):
            node.roster = body["roster"]
            st.put("roster", node.roster)
        st.put("pigeon_seen_map", body.get("seen") or {})
        if int(body.get("epoch", 0)) > node.epoch:
            node.epoch = int(body["epoch"])
            st.put("epoch", node.epoch)
    if body.get("settings"):
        node._keep_settings(body["settings"])
    _answers(node, body, rep)
    since = int(node.since or 0)
    after, hi = int(body.get("after") or 0), int(body.get("hi") or 0)
    if hi <= since:
        return "had"
    if after > since:
        return "waiting"
    entries = [e for e in body.get("ops") or [] if int(e.get("seq") or 0) > since]
    if entries:
        node.apply_entries(entries)
        rep["applied"] += len(entries)
    node.since = hi
    st.put("since", hi)
    node.changed.emit()
    return "applied"


def _answers(node, body, rep):
    """The host's answers to this computer's changes: a refused change goes back to the room's copy, a conflict is
    kept for the author, a held delete is not sent again."""
    st = node.store
    held = set(st.get("pigeon_held") or [])
    for uid in (body.get("refused") or {}).get(node.me_id) or []:
        if st.outbox_op(uid):
            node._revert(uid)
            st.drop_outbox(uid)
    for uid, x in ((body.get("results") or {}).get(node.me_id) or {}).items():
        if not st.outbox_op(uid):
            continue
        status = x.get("status")
        if status == "held":
            held.add(uid)
            continue
        if x.get("conflicts"):
            node._keep_conflict(x)
            rep["conflicts"] += 1
        if status == "refused":
            node._revert(uid)
        if status in ("refused", "conflict"):
            st.drop_outbox(uid)
    held = {u for u in held if st.outbox_op(u)}
    st.put("pigeon_held", sorted(held))


def _retry_waiting(node, rep):
    st = node.store
    moved = True
    while moved:
        moved = False
        for pid, blob in _q(st, "SELECT id, blob FROM pigeon_carry WHERE waiting=1 ORDER BY after"):
            try:
                body = unseal(node, bytes(blob))
            except PigeonError:
                _x(st, "DELETE FROM pigeon_carry WHERE id=?", (pid,))
                continue
            r = _member_take(node, body, rep)
            if r != "waiting":
                _x(st, "UPDATE pigeon_carry SET waiting=0 WHERE id=?", (pid,))
                _mark_seen(st, pid)
                moved = True


def prune(node):
    """Drop the carried packs nobody needs any more. -> how many."""
    st = _ready(node.store)
    now = time.time()
    n = 0
    with st._lock:
        n += st.con.execute("DELETE FROM pigeon_carry WHERE expires<?", (now,)).rowcount
        n += st.con.execute("DELETE FROM pigeon_seen WHERE ts<?", (now - 2 * EXPIRY,)).rowcount
    for pid, uids in _q(st, "SELECT id, uids FROM pigeon_carry WHERE kind='up'"):
        u = json.loads(uids or "[]")
        if u and not st.missing(u):        # all its changes are in the stream: delivered
            _x(st, "DELETE FROM pigeon_carry WHERE id=?", (pid,))
            n += 1
    seen = st.get("pigeon_seen_map") or {}
    roster = node.roster or st.get("roster") or []
    members = [m["id"] for m in roster if not m.get("host") and m.get("state") != "out"]
    if members and all(m in seen or m == node.me_id for m in members):
        low = min([int(seen[m]) for m in members if m in seen] + [int(node.since or 0)])
        n += _q_count(st, "DELETE FROM pigeon_carry WHERE kind='down' AND waiting=0 AND hi<=?", (low,))
    extra = _q(st, "SELECT id FROM pigeon_carry ORDER BY made DESC LIMIT -1 OFFSET ?", (MAX_CARRY,))
    for (pid,) in extra:
        _x(st, "DELETE FROM pigeon_carry WHERE id=?", (pid,))
        n += 1
    return n


def _q_count(store, sql, args):
    with store._lock:
        return store.con.execute(sql, args).rowcount
