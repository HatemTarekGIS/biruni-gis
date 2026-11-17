"""The host of a room: the one computer that writes the shared files.

Members never write a file someone else has open (that is what breaks a GeoPackage shared on a
network folder). They send their changes here; the hub applies them one at a time, in order,
to the host's own layers, joins concurrent edits of the same feature (merge.py), gives every
accepted change a sequence number and a new feature version, and hands the ordered stream to
everyone, the host's own map included.

The HTTP server answers in its own threads; everything that touches a layer runs in Biruni's
main thread (Hub.drain), like the map service of a live share.

Requests (all sealed with the room key, wire.py):
  hello     join (the host approves a new member once) -> the room, a member token, a block of ids
  snapshot  the room's files at one point of the stream, in pieces
  push      changes -> for each: applied / joined with another change / conflict / refused
  pull      waits for the changes after a sequence number, with who is where on the map
  presence  this member's pointer, view and work area
  ids       a new block of feature ids
  have      which of these changes the stream lacks (sent again after a change of host)
  work_*    team computing: the board of tiles that members who share their computer take (team_compute/jobs.py)
  vault_*   team vault: mail of shards and signed requests, and what each member keeps (team_vault/net.py)
"""
import hashlib
import json
import os
import queue
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from ..local_server import ExclusiveServer
from ..secure import crypto
from . import area, features, merge, rules, wire

BLOCK = 1_000_000          # feature ids handed out at a time
PULL_WAIT = 20             # seconds a pull waits for news
PRESENCE_TTL = 20          # seconds a pointer stays on the map without news
PIECE = 1 << 20            # bytes per snapshot piece (small: a weak connection that breaks loses little)
GATHER = 0.12              # seconds a pull waits after news for the changes of the same moment
FLUSH_GAP = 0.15           # seconds between two writes to the disk: changes arriving meanwhile are written together
MAX_POINTERS = 40          # pointers of others sent to one member
MAX_EXTRA = 300 << 20      # other local files of the map sent to members, at most
MAX_BASE_EXTRA = 100 << 20  # vector extra files sent in a folder base package, at most


class _Job:
    def __init__(self, kind, msg, member=None):
        self.kind = kind
        self.msg = msg
        self.member = member
        self.done = threading.Event()
        self.result = None
        self.tries = 0

    def finish(self, result):
        self.result = result
        self.done.set()


ROOM_SETTINGS = ("approve", "default_rights", "external", "constitution", "number_grants", "host_grants",
                 "field_locks")  # [I06] rules.py


def room_settings(store):
    """The room's settings, sent to every member so that a member who takes over as host keeps them (whether
    joining needs approval, the rights of new members, whether the room is reachable from outside)."""
    return {k: store.get(k) for k in ROOM_SETTINGS if store.get(k) is not None}


def own_block(member_id, n):
    """The n-th block of feature ids of a member, worked out from its id alone. No host keeps a counter, so a new
    host after a takeover - or a member working offline - can never hand out ids already in use (a host-kept
    counter started again from the beginning on the new host, and two members got the same ids: every save of
    one of them then failed). 2^42 possible blocks of BLOCK ids fit in a GeoPackage's 64-bit ids; for 100 members
    the chance two blocks meet is about one in a billion."""
    h = int(hashlib.sha256(f"biruni-team-block|{member_id}|{n}".encode("utf-8")).hexdigest()[:16], 16)
    start = (1000 + h % (2 ** 42)) * BLOCK
    return [start, start + BLOCK]


def sign_body(op):
    keys = ("uid", "layer", "fid", "kind", "state", "base_ver", "author", "ts")
    body = {k: op.get(k) for k in keys}
    if op.get("src") is not None:   # where it was drawn from (provenance): signed too, when there is one, so
        body["src"] = op["src"]     # nobody can say afterwards it was drawn from another image
    return crypto.canonical(body)


def _src(src):
    """The provenance a member sent, kept small: {"img": [up to 3 short names], "scale": number}, or None."""
    if not isinstance(src, dict):
        return None
    out = {}
    img = src.get("img")
    if isinstance(img, list):
        out["img"] = [str(x)[:80] for x in img[:3] if isinstance(x, str)]
    if isinstance(src.get("scale"), (int, float)):
        out["scale"] = int(src["scale"])
    return out or None


class Hub(QObject):
    joinRequested = pyqtSignal(dict)       # a new member waits for approval
    deletesHeld = pyqtSignal(str)          # member id: its deletions wait for the host's answer
    changed = pyqtSignal()                 # members, presence or the stream changed

    def __init__(self, store, keys, layers, me, epoch=1, parent=None):
        super().__init__(parent)
        self.store = store
        self.keys = keys
        self.sealer = wire.Sealer(keys.key)
        self._nonces = {}                # member id -> (nonce, time) of a join waiting for its proof
        self.announce = None             # info -> signed info (set by the node: the host's own key)
        self.rekey = None                # {"at": time, "boxes": {member id: [eph hex, blob hex]}} while the code changes
        self._settings = (0.0, {})       # (when read, room settings)
        self._deletes = {}               # member id -> times of its last deletions (the guard)
        self.held = {}                   # member id -> [(op, member)] deletions waiting for the host's answer
        self._guard_off = False
        self.refused_for = {}            # member id -> [uids] of held deletions the host refused (told at its pull)
        self._areas = area.Checker()     # members' areas of work, made ready once per layer
        self._rules = (None, {}, [])     # [I06] (settings seen, verified constitution layers, verified grants)
        self._locks = (None, {})         # ((field_locks dump, roster_v), {layer_key: [field names]})
        self._nonce_lock = threading.Lock()
        self.layers = layers            # {room layer key: QgsVectorLayer}
        self.me = me                    # the host's own member dict
        self.epoch = epoch
        self.jobs = queue.Queue()
        self.deferred = []
        self.presence = {}
        self.waiting = {}               # member id -> request, until the host decides
        self.decided = {}               # member id -> "ok" | "refused"
        self.auto_accept = False
        self.open_door = True
        self.snapshot = None            # (seq, folder, files)
        self.base_pkg = None            # (seq, folder, info) for folder base packages
        self._snap_time = 0.0
        self._old_snaps = {}            # folder -> time of its last download (kept while someone downloads it)
        self.commit_now = lambda key: None   # set by the node: save the host's own edits on that layer now
        self.applied = None             # set by the node: called with each stream entry on the host
        self.cond = threading.Condition()
        self.server = None
        self.beacon = None
        self.port = None
        self.started = time.time()
        self.roster_v = 1
        self._roster = None
        self._tokens = None
        self.max_ops = 400               # changes written per turn of the main thread
        self._flushed = 0.0
        self.notice = None               # {"text", "ts"}: the host's message to everyone
        self.frozen = False              # maintenance: nobody but the host edits
        self.stats = {"bytes_in": 0, "bytes_out": 0, "requests": 0, "merged": 0, "conflicts": 0, "refused": 0,
                      "apply_ms": deque(maxlen=500), "times": deque(maxlen=20000), "by": {}}
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self.drain)

    # ---- start / stop -------------------------------------------------------------------------------
    def start(self, port=wire.PORT, bind="0.0.0.0"):
        hub = self
        for p in range(port, port + 20):
            try:
                self.server = _Server((bind, p), _Handler)
                break
            except OSError:
                continue
        if self.server is None:
            raise OSError("no free port for the room")
        self.server.hub = hub
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.beacon = wire.Beacon(self.keys, self.sealer, self.where)
        self.beacon.start()
        self._timer.start()
        return self.port

    def stop(self):
        self._timer.stop()
        if self.beacon is not None:
            self.beacon.stop()
            self.beacon = None
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        while True:
            try:
                self.jobs.get_nowait().finish({"error": "the room was closed", "status": 503})
            except queue.Empty:
                break
        for j in self.deferred:
            j.finish({"error": "the room was closed", "status": 503})
        self.deferred = []
        with self.cond:
            self.cond.notify_all()
        for folder in [self.snapshot[1]] if self.snapshot else []:
            shutil.rmtree(folder, ignore_errors=True)
        for folder in list(self._old_snaps):
            shutil.rmtree(folder, ignore_errors=True)
        self._old_snaps = {}
        self.snapshot = None
        if self.base_pkg:
            shutil.rmtree(self.base_pkg[1], ignore_errors=True)
            self.base_pkg = None

    def where(self):
        now = time.time()
        on = sum(1 for k, v in list(self.presence.items()) if k != self.me.get("id") and now - v["ts"] < 30)
        info = {"port": self.port, "epoch": self.epoch, "rank": self.me.get("rank", 0), "head": self.store.head(),
                "name": self.store.get("room_name", ""), "host": self.me.get("name", ""), "since": self.started,
                "on": on, "hid": self.me.get("id"), "sid": self.store.get("sid")}
        return self.announce(info) if self.announce else info

    # ---- members --------------------------------------------------------------------------------------
    def member_by_token(self, token):
        if not token:
            return None
        if token == self.me.get("token"):
            return self.me
        tokens = self._tokens
        if tokens is None or tokens[0] != self.roster_v:
            tokens = (self.roster_v, {m["token"]: m for m in self.store.members()
                                      if m.get("token") and m.get("state") == "in"})
            self._tokens = tokens
        return tokens[1].get(token)

    def _members_changed(self):
        self.roster_v += 1
        self._roster = None

    def hello(self, msg):
        """A computer asks to join (server thread)."""
        cid, name, pub = str(msg.get("id", ""))[:64], str(msg.get("name", ""))[:80], str(msg.get("pub", ""))[:128]
        if not cid or not pub:
            return {"status": "refused"}
        # proof of the private key: a member's id and public key are known to every member (the roster), so they
        # alone must never open the room - the joining computer signs a nonce the host just made
        if not self._proved(cid, pub, msg):
            nonce = secrets.token_hex(16)
            with self._nonce_lock:
                now = time.time()
                self._nonces = {k: v for k, v in self._nonces.items() if now - v[1] < 120}
                self._nonces[cid] = (nonce, now)
            return {"status": "prove", "nonce": nonce}
        for m in self.store.members():
            if m["id"] == cid:
                if m["pub"] != pub:
                    return {"status": "refused", "why": "another computer uses this name"}
                if m.get("state") == "out":
                    return {"status": "refused", "why": "removed"}
                m["name"] = name or m["name"]
                m["seen"] = time.time()
                m["token"] = secrets.token_hex(16)   # a new token for each session: an old one stops working
                m["state"] = "in"
                self.store.save_member(m)
                self._keep_box_key(cid, msg)
                self._members_changed()
                return self._welcome(m)
        if self.decided.get(cid) == "refused":
            return {"status": "refused"}
        if not self.open_door:
            return {"status": "refused", "why": "closed"}
        if self.decided.get(cid) != "ok" and not self.auto_accept:
            if cid not in self.waiting:
                req = {"id": cid, "name": name, "pub": pub, "fingerprint": crypto.fingerprint(bytes.fromhex(pub)),
                       "ts": time.time()}
                self.waiting[cid] = req
                self.jobs.put(_Job("ask", req))
            return {"status": "waiting"}
        self.waiting.pop(cid, None)
        m = self._admit(cid, name, pub, msg)
        return self._welcome(m)

    def _admit(self, cid, name, pub, msg):
        self.waiting.pop(cid, None)
        members = self.store.members()
        rank = max([m["rank"] for m in members] + [self.me.get("rank", 0)]) + 1
        default = self.store.get("default_rights", "edit")
        m = {"id": cid, "name": name, "pub": pub, "rank": rank,
             "rights": {k: default for k in self.layers}, "token": secrets.token_hex(16),
             "joined": time.time(), "seen": time.time(), "state": "in"}
        self.store.save_member(m)
        self._keep_box_key(cid, msg)
        self._members_changed()
        self.jobs.put(_Job("note", {}))
        return m

    def folder_join(self, req):
        """A join request arrived through the shared folder (already unsealed and signature-verified).

        Returns 'ok', 'waiting', or 'refused'.
        """
        cid = str(req.get("maker") or req.get("id") or "")[:64]
        name = str(req.get("name") or "")[:80]
        pub = str(req.get("pub") or "")[:128]
        if not cid or not pub:
            return "refused"
        for m in self.store.members():
            if m["id"] == cid:
                if m["pub"] != pub or m.get("state") == "out":
                    return "refused"
                m["name"] = name or m["name"]
                m["seen"] = time.time()
                m["state"] = "in"
                self.store.save_member(m)
                self._keep_box_key(cid, req)
                self._members_changed()
                return "ok"
        if self.decided.get(cid) == "refused":
            return "refused"
        if not self.open_door:
            return "refused"
        if self.decided.get(cid) != "ok" and not self.auto_accept:
            if cid not in self.waiting:
                ask = {"id": cid, "name": name, "pub": pub,
                       "fingerprint": crypto.fingerprint(bytes.fromhex(pub)), "ts": time.time()}
                self.waiting[cid] = ask
                self.jobs.put(_Job("ask", ask))
            return "waiting"
        self._admit(cid, name, pub, req)
        return "ok"

    def settings(self):
        """The room's settings for the members, read again every few seconds: every pull carries them, and a
        hundred members reading the database on each pull slowed the host's window down."""
        now = time.time()
        if now - self._settings[0] > 5:
            self._settings = (now, room_settings(self.store))
        return self._settings[1]

    def _keep_box_key(self, cid, msg):
        xpub = str(msg.get("xpub") or "")
        if len(xpub) == 64:
            keys = self.store.get("box_keys", {}) or {}
            if keys.get(cid) != xpub:
                keys[cid] = xpub
                self.store.put("box_keys", keys)

    def seal_new_code(self, code, at):
        """Seal ``code`` for each member still in the room (a removed member gets nothing). -> how many."""
        keys = self.store.get("box_keys", {}) or {}
        boxes = {}
        for m in self.store.members():
            xpub = keys.get(m["id"])
            if m.get("state") == "out" or not xpub:
                continue
            eph, blob = crypto.box_seal(bytes.fromhex(xpub), code.encode("utf-8"), wire.rekey_aad(m["id"]))
            boxes[m["id"]] = [eph.hex(), blob.hex()]
        self.rekey = {"at": at, "boxes": boxes}
        return len(boxes)

    def _proved(self, cid, pub, msg):
        """True when ``msg`` carries the nonce this host gave ``cid`` (once, within two minutes) signed with the
        private key of ``pub``."""
        nonce, proof = msg.get("nonce"), msg.get("proof")
        if not nonce or not proof:
            return False
        with self._nonce_lock:
            given = self._nonces.get(cid)
            if not given or given[0] != nonce or time.time() - given[1] > 120:
                return False
            del self._nonces[cid]               # a nonce opens one session only
        try:
            return crypto.verify(bytes.fromhex(pub), bytes.fromhex(str(proof)),
                                 wire.hello_proof(self.keys.tag, cid, nonce))
        except (ValueError, TypeError):
            return False

    def decide(self, cid, accept):
        self.decided[cid] = "ok" if accept else "refused"
        self.waiting.pop(cid, None)
        self.changed.emit()

    def _welcome(self, m):
        return {"status": "ok", "member": {k: m[k] for k in ("id", "name", "rank", "rights", "token")},
                "room": self.room_info(), "block": self.alloc_block()}

    def room_info(self):
        return {"name": self.store.get("room_name", ""), "layers": self.store.get("layers", {}),
                "crs": self.store.get("crs", ""), "extent": self.store.get("extent"), "epoch": self.epoch,
                "head": self.store.head(), "host": self.me.get("name", ""), "host_id": self.me.get("id"),
                "members": self.roster(), "sid": self.store.get("sid"), "settings": self.settings()}

    def roster(self):
        if self._roster is not None and self._roster[0] == self.roster_v:
            return self._roster[1]
        box_keys = self.store.get("box_keys", {}) or {}
        out = [{"id": self.me["id"], "name": self.me["name"], "rank": self.me.get("rank", 0), "host": True,
                "pub": self.me.get("pub"), "xpub": self.me.get("xpub"),
                "rights": {k: "edit" for k in self.layers}, "state": "in"}]
        for m in self.store.members():
            if m["id"] != self.me["id"]:
                d = {k: m.get(k) for k in ("id", "name", "rank", "rights", "pub", "state")}
                d["xpub"] = box_keys.get(m["id"])
                out.append(d)
        self._roster = (self.roster_v, out)
        return out

    def set_rights(self, cid, layer_key, right):
        for m in self.store.members():
            if m["id"] == cid:
                m["rights"][layer_key] = right
                self.store.save_member(m)
        self._members_changed()
        self._bump()

    def set_area(self, cid, value):
        """A member's area of work ({"wkt", "crs"} from area.make), or None: anywhere."""
        for m in self.store.members():
            if m["id"] == cid:
                rights = dict(m.get("rights") or {})
                if value:
                    rights[area.KEY] = value
                else:
                    rights.pop(area.KEY, None)
                m["rights"] = rights
                self.store.save_member(m)
        self._members_changed()
        self._bump()

    def _area_refuse(self, member, key, op, cur):
        """"outside your area" when the feature is not inside the member's area before or after the change."""
        if member is self.me:
            return ""
        a = area.of(member.get("rights"))
        if a is None:
            return ""
        lyr = self.layers.get(key)
        after = None if op.get("kind") == "del" else (op.get("state") or {})
        for st in (cur, after):
            if st is not None and not self._areas.inside(a, lyr, st):
                return "outside your area"
        return ""

    def remove_member(self, cid):
        for m in self.store.members():
            if m["id"] == cid:
                m["state"] = "out"
                m["token"] = None
                self.store.save_member(m)
        self._members_changed()
        self.presence.pop(cid, None)
        self._bump()

    def alloc_block(self):
        start = self.store.get("next_block", BLOCK)
        self.store.put("next_block", start + BLOCK)
        return [start, start + BLOCK]

    # ---- the stream -----------------------------------------------------------------------------------
    def _bump(self):
        with self.cond:
            self.cond.notify_all()
        self.changed.emit()

    def wait_news(self, since, wait):
        """Wait until the stream passes since (True) or the time is up (False)."""
        end = time.time() + wait
        with self.cond:
            while self.store.head() <= since and time.time() < end and self.server is not None:
                self.cond.wait(min(1.0, max(0.05, end - time.time())))
        return self.store.head() > since

    def visible(self, member, msg):
        """The pointers a member is shown: the others in or near its view (at most MAX_POINTERS, editors first),
        not the whole room - with a hundred members that is what keeps the answers small."""
        now = time.time()
        view = msg.get("view")
        after = float(msg.get("pts") or 0)
        out = []
        for k, v in list(self.presence.items()):
            if k == member["id"] or now - v["ts"] >= PRESENCE_TTL or v["ts"] <= after:
                continue
            c = v.get("c")
            if view and c:
                w, h = view[2] - view[0], view[3] - view[1]
                if not (view[0] - w * 0.25 <= c[0] <= view[2] + w * 0.25
                        and view[1] - h * 0.25 <= c[1] <= view[3] + h * 0.25):
                    continue
            out.append((0 if v.get("edit") else 1, -v["ts"], k, v))
        out.sort(key=lambda t: t[:2])
        return {k: v for _, _, k, v in out[:MAX_POINTERS]}

    def _entry(self, op, prev, state, ver, note=None):
        return {"seq": None, "uid": op["uid"], "epoch": self.epoch, "author": op["author"],
                "name": op.get("name", ""), "ts": op.get("ts", time.time()), "layer": op["layer"],
                "fid": int(op["fid"]),
                # a change of the layer's fields keeps its kind (it was written as a feature change, so no member
                # ever got a new field)
                "kind": "field" if op.get("kind") == "field" else ("del" if state is None else "put"),
                "state": state, "prev": prev,
                "ver": ver, "sig": op.get("sig"), "note": note or op.get("note"), "src": _src(op.get("src"))}

    def _kept(self, entries):
        """Entries now in the stream: numbers, the host's map, one wake-up for every waiting member."""
        for entry in entries:
            entry["seq"] = entry["local_seq"]
            self.stats["times"].append(time.time())
            self.stats["by"][entry["author"]] = self.stats["by"].get(entry["author"], 0) + 1
            if entry["note"] == "merged":
                self.stats["merged"] += 1
            if self.applied:
                self.applied(entry)
        if entries:
            self._bump()

    def record(self, op, prev, state, ver, note=None):
        entry = self._entry(op, prev, state, ver, note)
        if self.store.append(entry):
            self._kept([entry])
        return entry

    def local(self, op, prev, state):
        """A change the host made in its own map (already in the file): into the stream."""
        ver = self.store.version(op["layer"], op["fid"]) + 1
        key, fid = op["layer"], int(op["fid"])
        lyr = self.layers.get(key)
        if int(self.me.get("rank", 99)) != 0 and prev and state:
            locked = self._locked(key)
            if locked:
                pa, sa = prev.get("a") or {}, state.get("a") or {}
                bad = [f for f in locked if f in sa and sa[f] != pa.get(f)]
                if bad:
                    ca = dict(sa)
                    for f in bad:
                        if f in pa:
                            ca[f] = pa[f]
                        else:
                            ca.pop(f, None)
                    state = dict(state, a=ca)
                    if lyr is not None:
                        features.write_state(lyr, fid, state)
                        lyr.triggerRepaint()
                    self._log_error("locked field: " + ", ".join(bad))
                    if state == prev:
                        return None
        flags = []
        if lyr is not None and prev and state and prev.get("g") != state.get("g"):
            batch = _Batch(self)
            _new_st, flags = self._propagate_shared_nodes(key, lyr, fid, self.me, prev, state, state, batch, op)
            batch.flush()
        return self.record(op, prev, state, ver, note=("; ".join(flags) if flags else None))

    # ---- the main thread ------------------------------------------------------------------------------
    def drain(self):
        """One turn of the main thread: the changes that arrived since the last turn are decided one by one (in
        order) and written together - one write per layer and one to the record, whatever their number - so a
        hundred people editing at once cost about what one does."""
        jobs, self.deferred = self.deferred, []
        n = sum(len(j.msg.get("ops") or []) for j in jobs if j.kind == "push")
        t_end = time.perf_counter() + 0.08
        while n < self.max_ops and time.perf_counter() < t_end:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
            if job.kind == "push":
                n += len(job.msg.get("ops") or [])
                jobs.append(job)
            else:
                self._run(job)
        pushes = [j for j in jobs if j.kind == "push"]
        if not pushes:
            return
        if n < self.max_ops and time.perf_counter() - self._flushed < FLUSH_GAP:
            self.deferred = pushes  # written with the next ones: one disk write instead of many
            return
        self._flushed = time.perf_counter()
        batch = _Batch(self)
        t0 = time.perf_counter()
        done = []
        for job in pushes:
            try:
                if self._push(job, batch):
                    done.append(job)
            except Exception as e:  # noqa: BLE001 - one bad request must not stop the room
                job.finish({"error": str(e)[:300], "status": 500})
        try:
            batch.flush()
        except Exception as e:  # noqa: BLE001
            for job in done:
                job.finish({"error": str(e)[:300], "status": 500})
            return
        if batch.count:
            per = (time.perf_counter() - t0) * 1000 / batch.count
            self.stats["apply_ms"].extend([per] * min(batch.count, 50))
        for job in done:
            for r in job.result_list:
                e = r.pop("_entry", None) if isinstance(r, dict) else None
                if e is not None:
                    if e.get("seq") is None:
                        r.update(status="refused", why=e.get("_why") or "cannot write")
                        r.pop("conflicts", None)
                    else:
                        r["seq"] = e["seq"]
            job.finish({"results": job.result_list, "head": self.store.head()})

    def _run(self, job):
        try:
            if job.kind == "ask":
                self.joinRequested.emit(job.msg)
                job.finish({})
            elif job.kind == "note":
                self._bump()
                job.finish({})
            elif job.kind == "push":
                self._push(job)
            elif job.kind == "snapshot":
                job.finish(self.make_snapshot())
        except Exception as e:  # noqa: BLE001 - one bad request must not stop the room
            job.finish({"error": str(e)[:300], "status": 500})

    def _push(self, job, batch):
        """Decide the job's changes into the batch. False when it waits for the host's own unsaved edit."""
        results = job.result_list if hasattr(job, "result_list") else []
        job.result_list = results
        ops = job.msg.get("ops") or []
        while len(results) < len(ops):
            op = ops[len(results)]
            r = self.decide_op(op, job.member, batch)
            if r == "later":
                job.tries += 1
                if job.tries < 400:  # about 10 s of the host's own unsaved edit; then it is joined as it is
                    self.deferred.append(job)
                    return False
                r = self.decide_op(op, job.member, batch, force=True)
            if isinstance(r, dict):
                if r.get("conflicts"):
                    self.stats["conflicts"] += 1
                if r.get("status") == "refused":
                    self.stats["refused"] += 1
            results.append(r)
        return True

    def apply_remote(self, op, member, force=False):
        """One change on its own (the host's own outbox after a take-over)."""
        batch = _Batch(self)
        r = self.decide_op(op, member, batch, force=force)
        batch.flush()
        if isinstance(r, dict):
            e = r.pop("_entry", None)
            if e is not None:
                if e.get("seq") is None:
                    r.update(status="refused", why=e.get("_why") or "cannot write")
                else:
                    r["seq"] = e["seq"]
        return r

    def _member(self, cid):
        """The roster entry of a member id (the host itself included), or None."""
        if cid and cid == self.me.get("id"):
            return self.me
        return next((m for m in self.store.members() if m["id"] == cid), None) if cid else None

    def _rights_refuse(self, member, key, op):
        """Why ``member`` may not make ``op`` on layer ``key`` (or ""): "view only", "cannot delete", etc."""
        if member is self.me:
            return ""
        if op.get("kind") == "field":
            return "only the host changes fields"
        right = (member.get("rights") or {}).get(key, "edit")
        if right == "view":
            return "view only"
        if right == "nodelete" and op.get("kind") == "del":
            return "cannot delete"
        if right == "data" and op.get("kind") == "del":
            return "data entry only: cannot delete"
        return ""

    def _hold(self, op, member):
        """The guard: a member deleting more than delete_limit features in a minute waits for the host to say yes
        (a mistake, or someone wiping the data on purpose, is stopped before it reaches everyone)."""
        limit = int(self.store.get("delete_limit", 20) or 0)
        if member is self.me or not limit or self._guard_off:
            return False
        mid = member["id"]
        if mid in self.held:
            self.held[mid].append((op, member))
            return True
        now = time.time()
        times = [t for t in self._deletes.get(mid, []) if now - t < 60]
        if len(times) >= limit:
            self.held[mid] = [(op, member)]
            self._deletes[mid] = []
            self.deletesHeld.emit(mid)
            return True
        times.append(now)
        self._deletes[mid] = times
        return False

    def release(self, member_id, allow):
        """The host's answer about a member's held deletions: carried out, or refused (the member's copy gets the
        features back). -> how many."""
        items = self.held.pop(member_id, [])
        if allow:
            self._guard_off = True
            try:
                for op, member in items:
                    self.apply_remote(op, member)
            finally:
                self._guard_off = False
        else:
            self.refused_for.setdefault(member_id, []).extend(op["uid"] for op, _m in items)
            self._bump()
        return len(items)

    @staticmethod
    def _signed(op, pub):
        try:
            return crypto.verify(bytes.fromhex(pub), bytes.fromhex(op.get("sig") or "00"), sign_body(op))
        except (ValueError, TypeError):
            return False

    def decide_op(self, op, member, batch, force=False):
        uid = str(op.get("uid", ""))
        if not uid:
            return {"uid": uid, "status": "refused", "why": "bad change"}
        if uid in batch.uids or self.store.has(uid):
            return {"uid": uid, "status": "ok", "dup": True}
        key = op.get("layer")
        lyr = self.layers.get(key)
        if lyr is None:
            return {"uid": uid, "status": "refused", "why": "no such layer"}
        if self.frozen and member is not self.me:
            return {"uid": uid, "status": "refused", "why": "frozen"}
        if op.get("replay"):
            # a change the previous host accepted, sent again by a member after a change of host. It keeps its
            # author - so it must carry that author's own signature, and that author must still be a member with
            # the right to edit the layer (anyone could otherwise write anything in anyone's name). "force" is
            # not signed: a replay never overrides.
            author = self._member(op.get("author"))
            if author is None or author.get("state") == "out" or not author.get("pub"):
                return {"uid": uid, "status": "refused", "why": "unknown author"}
            if not self._signed(op, author["pub"]):
                return {"uid": uid, "status": "refused", "why": "bad signature"}
            refused = self._rights_refuse(author, key, op)
            if refused:
                return {"uid": uid, "status": "refused", "why": refused}
            op.pop("force", None)
            force = False
            op["name"] = author.get("name", "")
            who = author
        else:
            refused = self._rights_refuse(member, key, op)
            if refused:
                return {"uid": uid, "status": "refused", "why": refused}
            pub = member.get("pub")
            if member is not self.me and not pub:
                return {"uid": uid, "status": "refused", "why": "bad signature"}
            if pub and not self._signed(op, pub):
                return {"uid": uid, "status": "refused", "why": "bad signature"}
            op["author"], op["name"] = member["id"], member.get("name", "")
            who = member
            if op.get("kind") == "del" and self._hold(op, member):
                return {"uid": uid, "status": "held"}
        fid = int(op["fid"])
        if features.is_pending(lyr, fid) and not force:
            self.commit_now(key)
            return "later"
        cur = batch.state(key, fid)
        cur_ver = batch.version(key, fid)
        base_ver = int(op.get("base_ver") or 0)
        refused = self._area_refuse(who, key, op, cur)
        if refused:
            return {"uid": uid, "status": "refused", "why": refused}
        data_only = who is not self.me and (who.get("rights") or {}).get(key, "edit") == "data"
        if data_only and cur is None:
            return {"uid": uid, "status": "refused", "why": "data entry only: cannot add features"}
        conflicts = []
        if op.get("kind") == "del":
            if cur is None:
                return {"uid": uid, "status": "ok", "gone": True}
            if cur_ver != base_ver and op.get("base") != cur and not op.get("force"):
                return self._conflict(op, cur, [{"what": "delete", "field": "", "mine": None, "theirs": cur.get("g")}])
            e = batch.write(op, cur, None, cur_ver + 1)
            return {"uid": uid, "status": "ok", "_entry": e}
        state = op.get("state") or {}
        merged = False
        if cur is None:
            if cur_ver and cur_ver != base_ver and not op.get("force"):  # deleted since the author's version
                return self._conflict(op, None, [{"what": "deleted", "field": "", "mine": state.get("g"),
                                                  "theirs": None}])
            result = state
        elif cur_ver == base_ver or op.get("force"):
            result = state
        else:
            # [I06] the room's signed merge constitution decides fields both sides changed (rules.py)
            lr, _g = self._room_rules()
            theirs_rank = 99
            hist = self.store.feature_history(key, fid)
            if hist:
                last = self._member(hist[-1].get("author"))
                theirs_rank = int((last or {}).get("rank", 99))
            result, conflicts = merge.merge_feature(op.get("base") or {}, state, cur, rules=lr.get(key),
                                                    mine_rank=int(who.get("rank", 99)), theirs_rank=theirs_rank)
            merged = True
        if data_only:
            if not features.same_geometry(result.get("g"), cur.get("g")):
                return {"uid": uid, "status": "refused", "why": "data entry only: the shape cannot change"}
            result = dict(result, g=cur.get("g"))
        result, why = self._locked_check(who, key, cur, result)
        if why:
            return {"uid": uid, "status": "refused", "why": why}
        flags = self._number_flags(key, lyr, fid, who, cur, result)
        # [I06 second piece] shared-node geometry propagation to incident neighbours
        node_flags = []
        if lyr is not None and result and state and (op.get("base") or cur) and not data_only:
            base_st = op.get("base") if op.get("base") and op.get("base").get("g") else cur
            result, node_flags = self._propagate_shared_nodes(key, lyr, fid, who, base_st, state, result, batch, op)
        flags.extend(node_flags)
        note = "merged" if merged else ("; ".join(flags) if flags else None)
        e = batch.write(op, cur, result, cur_ver + 1, note=note)
        out = {"uid": uid, "status": "merged" if merged else "ok", "_entry": e}
        if flags:
            out["flags"] = flags
        if conflicts:
            out.update(self._conflict(op, cur, conflicts, applied=True))
        return out

    # ---- [I06 second piece] shared-node geometry propagation ------------------------------------------
    def _neighbours_at_node(self, key, lyr, fid, pt, batch, tol=None):
        """[(neighbour_fid, neighbour_state)] for features in key touching (pt[0], pt[1])."""
        from qgis.core import QgsRectangle, QgsFeatureRequest, QgsGeometry

        if tol is None:
            tol = merge.layer_tolerance(lyr.crs() if lyr else None)
        rect = QgsRectangle(pt[0] - tol, pt[1] - tol, pt[0] + tol, pt[1] + tol)
        out = []
        seen = set()
        try:
            req = QgsFeatureRequest().setFilterRect(rect)
            for f in lyr.getFeatures(req):
                nfid = f.id()
                if nfid == fid:
                    continue
                st = batch.state(key, nfid)
                if st and st.get("g") and nfid not in seen:
                    g = QgsGeometry()
                    g.fromWkb(bytes.fromhex(st["g"]))
                    if any(merge._pt_dist((v.x(), v.y()), pt) <= tol for v in g.vertices()):
                        seen.add(nfid)
                        out.append((nfid, st))
        except Exception as e:
            self._log_error(f"shared-node spatial query: {e}")
        for (k_layer, nfid), (_old_s, cur_s) in batch.cur.items():
            if k_layer == key and nfid != fid and nfid not in seen and cur_s and cur_s.get("g"):
                try:
                    g = QgsGeometry()
                    g.fromWkb(bytes.fromhex(cur_s["g"]))
                    if any(merge._pt_dist((v.x(), v.y()), pt) <= tol for v in g.vertices()):
                        seen.add(nfid)
                        out.append((nfid, cur_s))
                except Exception as e:
                    self._log_error(f"shared-node batch check: {e}")
        return sorted(out, key=lambda x: x[0])

    def _topo_check(self, lyr, fid, hex_g, batch, key):
        """Check if moving a node produced an overlap with existing neighbour polygons."""
        from qgis.core import QgsGeometry, QgsFeatureRequest, QgsWkbTypes

        if not hex_g:
            return ""
        g = QgsGeometry()
        g.fromWkb(bytes.fromhex(hex_g))
        if g.isNull() or g.type() != QgsWkbTypes.PolygonGeometry:
            return ""
        try:
            req = QgsFeatureRequest().setFilterRect(g.boundingBox())
            for f in lyr.getFeatures(req):
                if f.id() == fid:
                    continue
                other_st = batch.state(key, f.id())
                if not other_st or not other_st.get("g"):
                    continue
                og = QgsGeometry()
                og.fromWkb(bytes.fromhex(other_st["g"]))
                if not og.isNull() and og.isGeosValid() and g.overlaps(og):
                    inter = g.intersection(og)
                    if not inter.isNull() and inter.area() > 0.0001:
                        return "topology: overlap detected"
        except Exception as e:
            self._log_error(f"shared-node topology check: {e}")
        return ""

    def _log_error(self, msg):
        self.stats["errors"] = self.stats.get("errors", 0) + 1
        errors = self.store.get("room_errors") or []
        errors.append({"ts": round(time.time(), 3), "text": str(msg)[:300]})
        self.store.put("room_errors", errors[-50:])
        try:
            self.changed.emit()
        except Exception:
            pass


    def _propagate_shared_nodes(self, key, lyr, fid, who, base_state, state_state, result_state, batch, op):
        """Propagate moved common corners to all incident neighbour features in the layer.
        Pins the node if it touches a locked neighbour (hook ready for when feature locks are implemented)
        or one outside the member's work area."""
        base_g = (base_state or {}).get("g")
        state_g = (state_state or {}).get("g")
        res_g = (result_state or {}).get("g")
        if not base_g or not state_g or not res_g or base_g == state_g:
            return result_state, []
        tol = merge.layer_tolerance(lyr.crs() if lyr else None)
        moves = merge.node_moves(base_g, state_g, tol=tol)
        if not moves:
            return result_state, []

        user_area = area.of(who.get("rights"))
        locks = self.store.get("locks") or {}
        flags = []
        effective_moves = []
        for old_xy, new_xy in moves:
            neighbours = self._neighbours_at_node(key, lyr, fid, old_xy, batch, tol=tol)
            # 1. Locks check: pin if any incident neighbour is locked by someone else
            locked_by_other = [n_fid for n_fid, _ in neighbours
                               if locks.get(f"{key}:{n_fid}") and locks.get(f"{key}:{n_fid}") != who.get("id")]
            if locked_by_other:
                flags.append(f"pinned shared corner: neighbour #{locked_by_other[0]} is locked")
                continue
            # 2. Red-team fix: pin if any incident neighbour is outside member's work area
            if user_area and any(not self._areas.inside(user_area, lyr, n_st) for n_fid, n_st in neighbours):
                flags.append(f"pinned shared corner: ({old_xy[0]:.2f}, {old_xy[1]:.2f})")
                continue
            effective_moves.append((old_xy, new_xy))

        if len(effective_moves) < len(moves):
            pinned = [m[0] for m in moves if m not in effective_moves]
            pinned_g, _cnt = merge.pin_nodes(res_g, pinned, tol=tol)
            result_state = dict(result_state, g=pinned_g)

        if not effective_moves:
            return result_state, flags

        for old_xy, new_xy in effective_moves:
            neighbours = self._neighbours_at_node(key, lyr, fid, old_xy, batch, tol=tol)
            handled_fids = set()
            for n_fid, n_st in neighbours:
                if not n_st or not n_st.get("g"):
                    continue
                handled_fids.add(n_fid)
                new_n_g, cnt = merge.move_nodes(n_st["g"], [(old_xy, new_xy)], tol=tol)
                if cnt > 0 and new_n_g != n_st["g"]:
                    n_ver = batch.version(key, n_fid)
                    n_new_st = dict(n_st, g=new_n_g)
                    topo_flag = self._topo_check(lyr, n_fid, new_n_g, batch, key)
                    n_op = {"uid": f"{op['uid']}:n:{n_fid}", "layer": key, "fid": n_fid,
                            "kind": "put", "author": op.get("author", who.get("id")),
                            "name": op.get("name", who.get("name", "")), "ts": op.get("ts", time.time())}
                    note_parts = ["shared-node"]
                    if topo_flag:
                        note_parts.append(topo_flag)
                    batch.write(n_op, n_st, n_new_st, n_ver + 1, note="; ".join(note_parts))

            # T-junction check: corners lying on an edge of a neighbour without an existing vertex
            try:
                from qgis.core import QgsRectangle, QgsFeatureRequest
                rect = QgsRectangle(old_xy[0] - tol * 10, old_xy[1] - tol * 10, old_xy[0] + tol * 10, old_xy[1] + tol * 10)
                for f in lyr.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                    n_fid = f.id()
                    if n_fid == fid or n_fid in handled_fids:
                        continue
                    n_st = batch.state(key, n_fid)
                    if not n_st or not n_st.get("g"):
                        continue
                    split_g, split_ok = merge.insert_and_move_node_on_edge(n_st["g"], old_xy, new_xy, tol=tol)
                    if split_ok and split_g != n_st["g"]:
                        n_ver = batch.version(key, n_fid)
                        n_new_st = dict(n_st, g=split_g)
                        n_op = {"uid": f"{op['uid']}:t:{n_fid}", "layer": key, "fid": n_fid,
                                "kind": "put", "author": op.get("author", who.get("id")),
                                "name": op.get("name", who.get("name", "")), "ts": op.get("ts", time.time())}
                        batch.write(n_op, n_st, n_new_st, n_ver + 1,
                                    note=f"shared-node: T-junction corner on neighbour #{n_fid}")
            except Exception as e:
                self._log_error(f"shared-node T-junction check: {e}")

        return result_state, flags



    # ---- [I06] the merge constitution and escrowed numbers (rules.py) ----------------------------------
    def _locked(self, key):
        seen = (rules.dump(self.store.get(rules.FKEY)), self.roster_v)
        if self._locks[0] != seen:
            self._locks = (seen, {})
        cache = self._locks[1]
        if key not in cache:
            cache[key] = rules.locked_fields(self.store, self._member, key)
        return cache[key]

    def _locked_check(self, who, key, cur, result):
        """Enforce owner-locked fields: refuse changes to existing features; clear ungranted values on new ones."""
        locked = self._locked(key)
        if int(who.get("rank", 99)) == 0 or not locked:
            return result, ""
        ra = result.get("a") or {}
        if cur is not None:
            ca = cur.get("a") or {}
            for f in locked:
                if f in ra and ra[f] != ca.get(f):
                    return result, "locked field: " + f
            return result, ""
        changed = None
        grants = None
        for f in locked:
            if f not in ra:
                continue
            v = ra[f]
            if v is None or v == "":
                continue
            if grants is None:
                grants = [d for d in self._room_rules()[1] if d["layer"] == key and d["member"] == who.get("id")]
            if not rules.in_ranges(v, [d for d in grants if d["field"] == f]):
                if changed is None:
                    changed = dict(ra)
                changed[f] = None
        if changed is not None:
            return dict(result, a=changed), ""
        return result, ""

    def _room_rules(self):
        """({layer: {field: rule}}, [grant docs]) verified once per change of the stored settings."""
        seen = (rules.dump(self.store.get(rules.CKEY)), rules.dump(self.store.get(rules.GKEY)),
                len(self.store.members()))
        if self._rules[0] != seen:
            c = rules.constitution(self.store, self._member)
            self._rules = (seen, dict(c.get("layers") or {}), rules.grants(self.store, self._member))
        return self._rules[1], self._rules[2]

    def _number_flags(self, key, lyr, fid, who, cur, result):
        """"number outside your range" / "number already used" for a numbering field the change set or changed."""
        _lr, docs = self._room_rules()
        docs = [d for d in docs if d["layer"] == key]
        if not docs or not result:
            return []
        flags = []
        for field in sorted({d["field"] for d in docs}):
            v = (result.get("a") or {}).get(field)
            if v is None or v == "" or (cur is not None and (cur.get("a") or {}).get(field) == v):
                continue
            mine = [d for d in docs if d["field"] == field and d["member"] == who.get("id")]
            if not rules.in_ranges(v, mine):
                flags.append(f"number outside your range: {field}={v}")
            if self._number_used(lyr, field, v, fid):
                flags.append(f"number already used: {field}={v}")
        return flags

    @staticmethod
    def _number_used(lyr, field, value, fid):
        from qgis.core import QgsExpression, QgsFeatureRequest

        if lyr is None or lyr.fields().indexOf(field) < 0:
            return False
        req = QgsFeatureRequest().setFilterExpression(
            f"{QgsExpression.quotedColumnRef(field)} = {QgsExpression.quotedValue(value)}")
        req.setFlags(QgsFeatureRequest.Flag.NoGeometry)
        return any(f.id() != fid for f in lyr.getFeatures(req))

    def _conflict(self, op, cur, items, applied=False):
        other = ""
        for e in reversed(self.store.feature_history(op["layer"], int(op["fid"]))[-30:]):
            if e.get("author") != op.get("author"):
                other = e.get("name", "")
                break
        return {"uid": op["uid"], "status": "merged" if applied else "conflict", "conflicts": items,
                "layer": op["layer"], "fid": int(op["fid"]), "mine": op.get("state"),
                "theirs": cur, "other": other, "ver": self.store.version(op["layer"], int(op["fid"]))}

    # ---- the room's files for a new member ------------------------------------------------------------
    def _copy_shared_gpkgs(self, folder):
        files, sources = {}, {}
        for key, lyr in self.layers.items():
            path = lyr.dataProvider().dataSourceUri().split("|")[0]
            if path not in sources:
                name = f"{len(sources) + 1}_{os.path.basename(path)}"
                src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                dst = sqlite3.connect(os.path.join(folder, name))
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
                dst.close()
                src.close()
                sources[path] = name
                files[name] = os.path.getsize(os.path.join(folder, name))
        layer_files = {key: sources[lyr.dataProvider().dataSourceUri().split("|")[0]]
                       for key, lyr in self.layers.items()}
        return files, layer_files

    def make_snapshot(self):
        head = self.store.head()
        # made again at most every SNAPSHOT_AGE: copying the room's files freezes the host's window, and a
        # joiner gets the changes made since from the stream anyway
        if self.snapshot and os.path.isdir(self.snapshot[1]) and (
                self.snapshot[0] == head or time.time() - self._snap_time < SNAPSHOT_AGE):
            return self._snapshot_info()
        self._snap_time = time.time()
        if self.snapshot:   # kept while someone still downloads it (deleting it stranded slow joiners)
            self._old_snaps[self.snapshot[1]] = time.time()
        for folder, used in list(self._old_snaps.items()):
            if time.time() - used > SNAPSHOT_AGE:
                shutil.rmtree(folder, ignore_errors=True)
                del self._old_snaps[folder]
        folder = tempfile.mkdtemp(prefix="biruni-room-")
        files, layer_files = self._copy_shared_gpkgs(folder)
        extra = self._extra_files(folder, files)
        with open(os.path.join(folder, "versions.json"), "w", encoding="utf-8") as f:
            json.dump({"head": head, "versions": self.store.all_versions(), "layer_files": layer_files,
                       "extra": extra}, f)
        files["versions.json"] = os.path.getsize(os.path.join(folder, "versions.json"))
        from . import snapshot
        snapshot.write_qlr(os.path.join(folder, "map.qlr"))
        files["map.qlr"] = os.path.getsize(os.path.join(folder, "map.qlr"))
        self.snapshot = (head, folder, files)
        return self._snapshot_info()

    def base_left_out(self):
        """Non-shared local layers omitted from a folder base package (without copying files)."""
        from qgis.core import QgsMapLayer, QgsProject
        from . import snapshot

        shared = {lyr.id() for lyr in self.layers.values()}
        total, left_out = 0, []
        for lid, paths in snapshot.local_files(shared).items():
            lyr = QgsProject.instance().mapLayer(lid)
            if lyr is not None and lyr.type() == QgsMapLayer.LayerType.RasterLayer:
                left_out.append({"name": lyr.name(), "why": "raster"})
                continue
            size = sum(os.path.getsize(p) for p in paths)
            if total + size > MAX_BASE_EXTRA:
                if lyr is not None:
                    left_out.append({"name": lyr.name(), "why": "too large"})
                continue
            total += size
        return left_out

    def make_base(self):
        """Vectors-only base package directory for folder joins (cached separately from self.snapshot)."""
        head = self.store.head()
        if self.base_pkg and os.path.isdir(self.base_pkg[1]) and self.base_pkg[0] == head:
            return self.base_pkg[2]
        if self.base_pkg:
            shutil.rmtree(self.base_pkg[1], ignore_errors=True)
            self.base_pkg = None
        folder = tempfile.mkdtemp(prefix="biruni-base-")
        files, layer_files = self._copy_shared_gpkgs(folder)
        left_out = []
        extra = self._extra_files(folder, files, vectors_only=True, left_out=left_out)
        with open(os.path.join(folder, "versions.json"), "w", encoding="utf-8") as f:
            json.dump({"head": head, "versions": self.store.all_versions(), "layer_files": layer_files,
                       "extra": extra}, f)
        files["versions.json"] = os.path.getsize(os.path.join(folder, "versions.json"))
        from . import snapshot
        snapshot.write_qlr(os.path.join(folder, "map.qlr"))
        files["map.qlr"] = os.path.getsize(os.path.join(folder, "map.qlr"))
        info = {"dir": folder, "head": head, "files": files, "layer_files": layer_files,
                "extra": extra, "left_out": left_out}
        self.base_pkg = (head, folder, info)
        return info

    def _extra_files(self, folder, files, vectors_only=False, left_out=None):
        """The map's other layers that are files on this computer, copied when small enough: {layer id: name}."""
        from qgis.core import QgsMapLayer, QgsProject
        from . import snapshot

        shared = {lyr.id() for lyr in self.layers.values()}
        limit = MAX_BASE_EXTRA if vectors_only else MAX_EXTRA
        total, out = 0, {}
        for lid, paths in snapshot.local_files(shared).items():
            lyr = QgsProject.instance().mapLayer(lid) if vectors_only else None
            if vectors_only and lyr is not None and lyr.type() == QgsMapLayer.LayerType.RasterLayer:
                if left_out is not None:
                    left_out.append({"name": lyr.name(), "why": "raster"})
                continue
            size = sum(os.path.getsize(p) for p in paths)
            if total + size > limit:
                if vectors_only and left_out is not None and lyr is not None:
                    left_out.append({"name": lyr.name(), "why": "too large"})
                continue
            sub = os.path.join(folder, "files", hashlib.sha1(lid.encode()).hexdigest()[:10])
            os.makedirs(sub, exist_ok=True)
            for p in paths:
                shutil.copy2(p, sub)
                rel = os.path.relpath(os.path.join(sub, os.path.basename(p)), folder).replace("\\", "/")
                files[rel] = os.path.getsize(p)
            out[lid] = os.path.relpath(os.path.join(sub, os.path.basename(paths[0])), folder).replace("\\", "/")
            total += size
        return out

    def backup(self, folder, keep=24):
        """A copy of the shared files as they are now (SQLite's online backup), in folder/<time>/."""
        dest = os.path.join(folder, time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(dest, exist_ok=True)
        done = set()
        for lyr in self.layers.values():
            path = lyr.dataProvider().dataSourceUri().split("|")[0]
            if path in done:
                continue
            done.add(path)
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            dst = sqlite3.connect(os.path.join(dest, os.path.basename(path)))
            src.backup(dst)
            dst.close()
            src.close()
        old = sorted(d for d in os.listdir(folder) if os.path.isdir(os.path.join(folder, d)))
        for d in old[:-keep]:
            shutil.rmtree(os.path.join(folder, d), ignore_errors=True)
        return dest

    def numbers(self):
        """What the control panel shows."""
        now = time.time()
        times = list(self.stats["times"])
        per_min = [0] * 30
        for t in times:
            age = int((now - t) // 60)
            if 0 <= age < 30:
                per_min[29 - age] += 1
        ms = list(self.stats["apply_ms"])
        online = [k for k, v in self.presence.items() if now - v["ts"] < PRESENCE_TTL]
        size = 0
        for lyr in self.layers.values():
            try:
                size = max(size, os.path.getsize(lyr.dataProvider().dataSourceUri().split("|")[0]))
            except OSError:
                pass
        return {"uptime": now - self.started, "per_min": per_min, "last_min": per_min[-1],
                "today": sum(1 for t in times if now - t < 86400), "head": self.store.head(),
                "apply_ms": (sum(ms) / len(ms)) if ms else 0.0, "apply_max": max(ms) if ms else 0.0,
                "queue": self.jobs.qsize() + len(self.deferred), "online": online,
                "merged": self.stats["merged"], "conflicts": self.stats["conflicts"],
                "refused": self.stats["refused"], "bytes_in": self.stats["bytes_in"],
                "bytes_out": self.stats["bytes_out"], "requests": self.stats["requests"], "by": dict(self.stats["by"]),
                "size": size, "waiting": list(self.waiting.values()), "epoch": self.epoch,
                "errors": self.store.get("room_errors") or []}

    def _snapshot_info(self):
        head, folder, files = self.snapshot
        return {"head": head, "files": files, "snap": os.path.basename(folder)}

    def piece(self, name, offset, snap=None):
        if not self.snapshot:
            raise wire.WireError("no snapshot", 404)
        folder = self.snapshot[1]
        if snap and snap != os.path.basename(folder):   # a download that began with an earlier copy
            folder = next((f for f in self._old_snaps if os.path.basename(f) == snap), None)
            if folder is None:
                raise wire.WireError("no such copy", 404)
            self._old_snaps[folder] = time.time()
        path = os.path.normpath(os.path.join(folder, name))
        if not path.startswith(os.path.normpath(folder) + os.sep) or not os.path.isfile(path):
            raise wire.WireError("no such file", 404)
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(int(offset))
            data = f.read(PIECE)
        return {"size": size, "data": crypto.b64(data)}


# ---- the changes of one turn, written together ----------------------------------------------------------
class _Batch:
    """The states of the features touched in one turn (read once from the file, then kept here), and the
    stream entries; flush() writes each layer's final states in one go and the entries in one transaction."""

    def __init__(self, hub):
        self.hub = hub
        self.cur = {}        # (layer key, fid) -> [state in the file, state now]
        self.vers = {}
        self.entries = []
        self.uids = set()
        self.count = 0

    def state(self, key, fid):
        k = (key, fid)
        if k not in self.cur:
            s = features.stored_state(self.hub.layers[key], fid)
            self.cur[k] = [s, s]
        return self.cur[k][1]

    def version(self, key, fid):
        k = (key, fid)
        if k not in self.vers:
            self.vers[k] = self.hub.store.version(key, fid)
        return self.vers[k]

    def write(self, op, prev, state, ver, note=None):
        k = (op["layer"], int(op["fid"]))
        self.state(*k)
        self.cur[k][1] = state
        self.vers[k] = ver
        e = self.hub._entry(op, prev, state, ver, note)
        self.entries.append(e)
        self.uids.add(op["uid"])
        self.count += 1
        return e

    def flush(self):
        failed = set()
        for key in {k for k, _ in self.cur}:
            lyr = self.hub.layers.get(key)
            if lyr is None:
                continue
            changes = {fid: (a, b) for (k, fid), (a, b) in self.cur.items() if k == key and a != b}
            if changes and not features.write_states(lyr, changes):
                failed.add(key)
                why = lyr.dataProvider().lastError() or "cannot write"
                for e in self.entries:
                    if e["layer"] == key:
                        e["_why"] = why
            elif changes:
                lyr.triggerRepaint()
        kept = [e for e in self.entries if e["layer"] not in failed]
        if kept:
            added = self.hub.store.append_many(kept)
            self.hub._kept([e for e, ok in zip(kept, added) if ok])


# ---- HTTP -------------------------------------------------------------------------------------------------
SNAPSHOT_AGE = 600             # seconds a copy of the room's files is served before a new one is made
MAX_CONNECTIONS = 600          # at once: a hundred members keep two or three each; more is someone flooding
MAX_MESSAGE = 4 << 20          # bytes of any message but a push of changes
MAX_PUSH = 64 << 20


def clean_presence(p):
    """A member's pointer as the others get it: only the known fields, numbers and short names - anything else
    a member sends would be relayed to forty others on every pull."""
    out = {}
    if not isinstance(p, dict):
        return out

    def nums(v, n):
        if isinstance(v, (list, tuple)) and len(v) == n and all(isinstance(x, (int, float)) for x in v):
            return [float(x) for x in v]
        return None
    c, e = nums(p.get("c"), 2), nums(p.get("e"), 4)
    if c:
        out["c"] = c
    if e:
        out["e"] = e
    edit = p.get("edit")
    if isinstance(edit, (list, tuple)):
        out["edit"] = [str(x)[:40] for x in edit[:20]]
    return out


class _Server(ExclusiveServer):
    """The host's server: each connection on its own thread - but not without end, and none waits forever (a
    connection that sends nothing would otherwise keep its thread: a few hundred of them stop the host)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)       # full: this one is turned away, the members already in go on
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # a member that closed Biruni or lost its network in the middle of a request
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60                   # seconds a connection may stay silent (a pull waits 20 s for news)

    def log_message(self, *a):
        pass

    def _send(self, status, obj):
        body = self.server.hub.sealer.seal(obj)
        self.server.hub.stats["bytes_out"] += len(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - only to say what this is to a browser
        body = b"Biruni GIS room\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        hub = self.server.hub
        n = int(self.headers.get("Content-Length") or 0)
        limit = MAX_PUSH if self.path.strip("/").split("?")[0] == "push" else MAX_MESSAGE
        if n <= 0 or n > limit:
            self.send_response(413)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        blob = self.rfile.read(n)
        hub.stats["bytes_in"] += n
        hub.stats["requests"] += 1
        try:
            msg = hub.sealer.open(blob) or {}
        except wire.WireError as e:
            body = str(e).encode()
            self.send_response(e.status or 403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        path = self.path.strip("/").split("?")[0]
        try:
            status, out = self._route(hub, path, msg)
        except wire.WireError as e:
            status, out = e.status or 400, {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            status, out = 500, {"error": str(e)[:300]}
        self._send(status, out)

    def _route(self, hub, path, msg):
        if path == "hello":
            return 200, hub.hello(msg)
        member = hub.member_by_token(msg.get("token"))
        if member is None:
            return 401, {"error": "not a member of this room"}
        member["seen"] = time.time()
        if path == "pull":
            since = int(msg.get("since") or 0)
            if hub.wait_news(since, min(float(msg.get("wait", PULL_WAIT)), PULL_WAIT)):
                time.sleep(GATHER)  # let the changes of the same moment come in one answer
            # read again after waiting: rights or an area given meanwhile reach the member in this answer (the entry
            # read before the wait is older than them)
            member = hub.member_by_token(msg.get("token")) or member
            ops = hub.store.since(since, 300)
            out = {"ops": ops, "head": hub.store.head(), "epoch": hub.epoch, "presence": hub.visible(member, msg),
                   "now": time.time(),
                   "notice": hub.notice, "frozen": hub.frozen, "rv": hub.roster_v, "rights": member.get("rights"),
                   "settings": hub.settings()}
            if hub.refused_for.get(member["id"]):
                out["refused"] = hub.refused_for.pop(member["id"])
            if hub.rekey and member["id"] in hub.rekey["boxes"]:
                out["rekey"] = {"at": hub.rekey["at"], "box": hub.rekey["boxes"][member["id"]]}
            if int(msg.get("rv") or 0) != hub.roster_v:
                out["members"] = hub.roster()
            return 200, out
        if path == "presence":
            p = clean_presence(msg.get("p"))
            p["ts"] = time.time()
            p["name"] = member.get("name", "")
            hub.presence[member["id"]] = p
            return 200, {}
        if path == "push":
            job = _Job("push", msg, member)
            hub.jobs.put(job)
            if not job.done.wait(60):
                return 503, {"error": "the host is busy"}
            r = job.result or {}
            return int(r.get("status", 200)) if "error" in r else 200, r
        if path == "snapshot":
            job = _Job("snapshot", msg)
            hub.jobs.put(job)
            if not job.done.wait(300):
                return 503, {"error": "the host is busy"}
            return 200, job.result
        if path == "piece":
            return 200, hub.piece(str(msg.get("name", "")), int(msg.get("offset") or 0), msg.get("snap"))
        if path == "ids":
            return 200, {"block": hub.alloc_block()}
        if path == "have":
            return 200, {"missing": hub.store.missing([str(u) for u in (msg.get("uids") or [])][:20000])}
        if path == "bye":
            hub.presence.pop(member["id"], None)
            hub._bump()
            return 200, {}
        if path.startswith("work_"):
            from ..team_compute import jobs as work

            return work.board_of(hub).route(path, msg, member)
        if path.startswith("vault_"):   # [team vault] the post office of the team vault (team_vault/net.py)
            from ..team_vault import net as vault_net

            return vault_net.board_of(hub).route(path, msg, member)
        return 404, {"error": "unknown request"}
