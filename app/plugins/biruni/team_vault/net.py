"""The team vault over a team room.

The room's host keeps a BOARD (in memory): a mailbox per member and what each member says it keeps. The board is
only a post office - everything it carries is either a shard nobody alone can read, or a request signed by the
owner; it cannot forge or read anything.

Every computer in a room runs a KEEPER (a thread): it takes its mail, keeps the shards others place on it (checked,
within the space its user gives), tells the board what it keeps, gives shards back to their owner when the owner
asks with a signed request, and - for its own user as an owner - places new shards, gathers shards for a restore and
repairs what was lost.

Requests (vault_* through the room, hub._route):
  vault_send    [{to, kind, meta, data}]    -> {"sent": n, "full": bool}   mail to members (flow control: full)
  vault_recv    {max}                        -> {"items": [...]}           this member's mail
  vault_have    {inv, free, quota, hold}     -> {}                         what this member keeps
  vault_status  {}                           -> {"me", "members", "live"}  who keeps what
"""
import base64
import collections
import json
import os
import shutil
import threading
import time

from . import core

PIECE = 768 << 10           # bytes of one piece of a shard in a message (sealed messages stay under 4 MiB)
BATCH = 3 << 20             # bytes of pieces sent in one request
BOARD_CAP = 160 << 20       # bytes of mail the host keeps at once
MAIL_TTL = 3600             # seconds a mail waits for its member
LIVE = 90                   # seconds after its last report a member still counts as online
HAVE_EVERY = 20             # seconds between two reports of what this computer keeps
ASK_TTL = 15 * 60           # seconds a signed request stays valid
RESEND_AFTER = 180          # seconds without an acknowledgement before a shard is placed again


class Board:
    def __init__(self, members_fn=None):
        self.members_fn = members_fn      # -> {id: name} of the room (None: anyone the room let in)
        self.mail = collections.defaultdict(collections.deque)
        self.bytes = 0
        self.inv = {}
        self._lock = threading.Lock()

    def route(self, path, msg, member):
        if not isinstance(msg, dict):
            return 400, {"error": "bad request"}
        fn = {"vault_send": self._send, "vault_recv": self._recv, "vault_have": self._have,
              "vault_status": self._status}.get(path)
        if fn is None:
            return 404, {"error": "unknown request"}
        with self._lock:
            self._expire()
            try:
                return 200, fn(msg, member)
            except (KeyError, TypeError, ValueError) as e:
                return 400, {"error": "bad request: " + str(e)[:100]}

    def _expire(self):
        now = time.time()
        for mid, q in self.mail.items():
            while q and now - q[0]["ts"] > MAIL_TTL:
                self.bytes -= len(q.popleft().get("data") or "")

    def _send(self, msg, member):
        known = self.members_fn() if self.members_fn else None
        sent = 0
        for it in msg.get("items") or []:
            to = str(it["to"])[:64]
            if known is not None and to not in known:
                raise ValueError("not a member of this room")
            data = str(it.get("data") or "")
            if self.bytes + len(data) > BOARD_CAP or len(self.mail[to]) > 4000:
                return {"sent": sent, "full": True}
            self.mail[to].append({"from": member["id"], "fname": str(member.get("name", ""))[:80],
                                  "kind": str(it["kind"])[:16], "meta": it.get("meta") or {}, "data": data,
                                  "ts": time.time()})
            self.bytes += len(data)
            sent += 1
        return {"sent": sent, "full": False}

    def _recv(self, msg, member):
        q = self.mail.get(member["id"])
        out, size, cap = [], 0, min(int(msg.get("max") or BATCH), BATCH)
        while q and (not out or size + len(q[0].get("data") or "") <= cap):
            it = q.popleft()
            ln = len(it.get("data") or "")
            self.bytes -= ln
            size += ln
            out.append({k: it[k] for k in ("from", "fname", "kind", "meta", "data")})
        return {"items": out, "more": bool(q)}

    def _have(self, msg, member):
        inv = msg.get("inv") or {}
        if not isinstance(inv, dict) or len(inv) > 20000:
            raise ValueError("inventory")
        self.inv[member["id"]] = {"name": str(member.get("name", ""))[:80], "seen": time.time(),
                                  "inv": {str(p)[:40]: dict({"o": str(v.get("o", ""))[:64],
                                                             "i": [int(x) for x in v.get("i", [])][:255]},
                                                            **({"d": float(v["d"])} if v.get("d") else {}))
                                          for p, v in inv.items()},
                                  "free": int(msg.get("free") or 0), "quota": int(msg.get("quota") or 0),
                                  "hold": bool(msg.get("hold"))}
        return {}

    def _status(self, msg, member):
        now = time.time()
        return {"me": member["id"], "live": LIVE,
                "members": {mid: dict(v, age=now - v["seen"]) for mid, v in self.inv.items()}}


def board_of(hub):
    b = getattr(hub, "_vault_board", None)
    if b is None:
        def members():
            try:
                return {m["id"]: m.get("name", "") for m in hub.roster()}
            except Exception:  # noqa: BLE001
                return None
        b = Board(members)
        hub._vault_board = b
    return b


class LinkError(Exception):
    pass


class _Transfer:
    """One shard file going to one member, piece by piece."""

    def __init__(self, to, kind, meta, blob):
        self.to, self.kind, self.meta, self.blob = to, kind, dict(meta), bytes(blob)
        self.parts = max(1, -(-len(self.blob) // PIECE))
        self.next = 0

    def piece(self, i):
        return dict(self.meta, part=i, parts=self.parts, size=len(self.blob))


class Keeper:
    """The vault's engine on one computer of a room. ``link_fn`` gives the current link to the board (it changes when
    the host does): an object with call(path, obj) -> dict, raising on refusal."""

    def __init__(self, vault, link_fn, name="", hold=True, have_every=HAVE_EVERY, tick=0.5):
        self.vault = vault
        self.link_fn = link_fn
        self.name = name
        self.hold = hold
        self.have_every = have_every
        self.tick = tick
        self.me = None
        self.status = {}
        self.status_ts = 0.0
        self.out = collections.deque()          # _Transfer waiting or under way
        self.incoming = {}                       # (from, kind, pack, idx, req) -> {"buf": bytearray, "got": set()}
        self.gathered = {}                       # req id -> {pack: {idx: (hdr, share, data, sig)}}
        self.pending = {}                        # (pack, idx) -> (member, ts) placed, not acknowledged yet
        self.refused = {}                        # (pack, idx) -> reason, from a holder that said no
        self.seen_nonces = {}
        self.last_error = ""
        self.received = 0
        self._last_have = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    # ---- running ----------------------------------------------------------------------------------------------
    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if not self.running:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="biruni-team-vault")
            self._thread.start()

    def stop(self, wait=5):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(wait)
        self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            try:
                busy = self.step()
            except Exception as e:  # noqa: BLE001 - the room comes and goes: try again later
                self.last_error = str(e)
                busy = False
            self._stop.wait(0.05 if busy else self.tick)

    def _call(self, path, obj):
        link = self.link_fn()
        if link is None:
            raise LinkError("no team room")
        return link.call(path, obj)

    def step(self):
        """One round: report, take mail, send. -> True when there is more to do right away."""
        now = time.time()
        if now - self._last_have >= self.have_every:
            self.report()
        r = self._call("vault_recv", {"max": BATCH})
        for it in r.get("items") or []:
            try:
                self._on_mail(it)
            except Exception as e:  # noqa: BLE001 - one bad mail never stops the rest
                self.last_error = str(e)
        sent = self._flush()
        return bool(r.get("more")) or sent

    def report(self):
        self._last_have = time.time()
        try:
            self.vault.purge_dropped()
        except OSError as e:
            self.last_error = str(e)
        inv = {p: dict({"o": v["owner"], "i": v["idx"]}, **({"d": v["purge"]} if v.get("purge") else {}))
               for p, v in self.vault.held().items()}
        used = sum(v["bytes"] for v in self.vault.held().values())
        quota = self.vault.quota if self.hold else 0
        self._call("vault_have", {"inv": inv, "free": max(0, quota - used), "quota": quota, "hold": self.hold})
        self.refresh()

    def refresh(self):
        st = self._call("vault_status", {})
        with self._lock:
            self.status, self.status_ts, self.me = st, time.time(), st.get("me")
        return st

    def _flush(self):
        """Send what waits, within the board's room. -> True when something went."""
        any_sent = False
        while True:
            with self._lock:
                if not self.out:
                    return any_sent
                items, size, full = [], 0, False
                for t in list(self.out):
                    while t.next < t.parts:
                        i = t.next
                        data = t.blob[i * PIECE:(i + 1) * PIECE]
                        est = len(data) * 4 // 3 + 4
                        if items and size + est > BATCH:
                            full = True
                            break
                        items.append((t, i, {"to": t.to, "kind": t.kind, "meta": t.piece(i),
                                             "data": base64.b64encode(data).decode("ascii")}))
                        size += est
                        t.next += 1
                    if full:
                        break
            if not items:
                return any_sent
            try:
                r = self._call("vault_send", {"items": [x[2] for x in items]})
            except Exception:
                r = {"sent": 0, "full": True, "error": True}
            n = int(r.get("sent") or 0)
            with self._lock:
                for t, i, _ in items[n:]:      # not taken: send again from there
                    t.next = min(t.next, i)
                for t in [t for t in self.out if t.next >= t.parts]:
                    self.out.remove(t)
            if r.get("error"):
                raise LinkError("the room did not take the shards")
            any_sent = any_sent or n > 0
            if r.get("full") or n < len(items):
                return any_sent

    def send_blob(self, to, kind, meta, blob):
        with self._lock:
            self.out.append(_Transfer(to, kind, meta, blob))

    # ---- mail -------------------------------------------------------------------------------------------------
    def _on_mail(self, it):
        kind, meta, frm = it["kind"], it.get("meta") or {}, it["from"]
        if kind in ("shard", "give"):
            blob = self._assemble(it)
            if blob is None:
                return
            if kind == "shard":
                self._on_shard(frm, it.get("fname", ""), meta, blob)
            else:
                self._on_give(meta, blob)
        elif kind == "ack":
            self._on_ack(frm, it.get("fname", ""), meta)
        elif kind == "ask":
            self._on_ask(frm, meta)
        elif kind == "drop":
            self._on_drop(meta)
        elif kind == "keep":
            self._on_keep(meta)

    def _assemble(self, it):
        meta = it["meta"]
        key = (it["from"], it["kind"], str(meta.get("pack")), int(meta.get("idx", -1)), str(meta.get("req", "")))
        parts, size, part = int(meta["parts"]), int(meta["size"]), int(meta["part"])
        if size > (1 << 31) or parts > 1 + size // PIECE + 1:
            return None
        with self._lock:
            slot = self.incoming.get(key)
            if slot is None or len(slot["buf"]) != size:
                slot = self.incoming[key] = {"buf": bytearray(size), "got": set(), "ts": time.time()}
            data = base64.b64decode(it.get("data") or "")
            slot["buf"][part * PIECE:part * PIECE + len(data)] = data
            slot["got"].add(part)
            if len(slot["got"]) < parts:
                return None
            del self.incoming[key]
            self.received += 1
            return bytes(slot["buf"])

    def _on_shard(self, frm, fname, meta, blob):
        ack = {"pack": meta.get("pack"), "idx": meta.get("idx")}
        if not self.hold:
            ack.update(ok=False, why="this computer does not keep shards for the team")
        else:
            try:
                pack, idx = self.vault.keep_shard(blob)
                ack.update(ok=True, pack=pack, idx=idx)
            except core.VaultError as e:
                ack.update(ok=False, why=str(e))
        self._call("vault_send", {"items": [{"to": frm, "kind": "ack", "meta": ack, "data": ""}]})
        if ack.get("ok"):
            self._last_have = 0  # tell the board soon

    def _on_ack(self, frm, fname, meta):
        key = (str(meta.get("pack")), int(meta.get("idx", -1)))
        with self._lock:
            self.pending.pop(key, None)
        if meta.get("ok"):
            self.vault.placed(key[0], key[1], frm, fname)
        else:
            with self._lock:
                self.refused[key] = (frm, str(meta.get("why", ""))[:200])

    def _check_signed(self, req):
        try:
            b, sig = req["b"], req["sig"]
            ok = core._crypto().verify(bytes.fromhex(b["owner"]), core.unb64u(sig), core.canonical(b))
        except Exception:  # noqa: BLE001
            return None
        now = time.time()
        if not ok or not (-300 < now - float(b.get("ts", 0)) < ASK_TTL):
            return None
        with self._lock:
            self.seen_nonces = {k: t for k, t in self.seen_nonces.items() if now - t < ASK_TTL + 300}
            if b.get("nonce") in self.seen_nonces:
                return None
            self.seen_nonces[b.get("nonce")] = now
        return b

    def _on_ask(self, frm, meta):
        b = self._check_signed(meta.get("req") or {})
        if b is None or b.get("kind") != "ask":
            return
        held = self.vault.held()
        for pack in b.get("packs") or []:
            info = held.get(str(pack))
            if not info or info["owner"] != b["owner"]:
                continue
            for idx in info["idx"]:
                self.send_blob(str(b["reply_to"]), "give", {"pack": pack, "idx": idx, "req": b["nonce"]},
                               self.vault.held_shard(pack, idx))

    def _on_give(self, meta, blob):
        req = str(meta.get("req", ""))
        with self._lock:
            found = self.gathered.get(req)
            if found is None:
                return
        try:
            core.add_shard(found, blob, owner=self.vault.pub)
        except core.VaultError as e:
            self.last_error = str(e)

    def _on_drop(self, meta):
        b = self._check_signed(meta.get("req") or {})
        if b is not None and b.get("kind") == "drop":
            self.vault.drop_held(b["owner"], [str(p) for p in b.get("packs") or []])   # deleted after the grace
            self._last_have = 0

    def _on_keep(self, meta):
        b = self._check_signed(meta.get("req") or {})
        if b is not None and b.get("kind") == "keep":
            self.vault.keep_held(b["owner"], [str(p) for p in b.get("packs") or []])
            self._last_have = 0

    # ---- what the team keeps ----------------------------------------------------------------------------------
    def live_members(self):
        st = self.status or {}
        return {mid: v for mid, v in (st.get("members") or {}).items() if v.get("age", 1e9) < st.get("live", LIVE)}

    def holders_of(self, pack, live_only=True):
        """{member: [idx…]} of the shards of one of my packs the members keep (as they report it)."""
        members = self.live_members() if live_only else (self.status or {}).get("members") or {}
        out = {}
        for mid, v in members.items():
            e = (v.get("inv") or {}).get(pack)
            if e and e.get("o") == self.vault.pub and e.get("i"):
                out[mid] = sorted(set(int(i) for i in e["i"]))
        return out

    def my_packs_in_room(self):
        """Every pack of mine some member reports (also on a new PC that knows nothing yet)."""
        out = {}
        for mid, v in ((self.status or {}).get("members") or {}).items():
            for p, e in (v.get("inv") or {}).items():
                if e.get("o") == self.vault.pub:
                    out.setdefault(p, set()).update(int(i) for i in e.get("i", []))
        return out

    # ---- owner: placing --------------------------------------------------------------------------------------
    def place(self, include_self=False):
        """Send the shards waiting in own/out to the members, each shard to the member that keeps the fewest of that
        pack. -> {"queued": n, "waiting": n, "why": text}"""
        self.refresh()
        out = self.vault.outbox()
        live = {m: v for m, v in self.live_members().items()
                if v.get("hold") and (include_self or m != self.me)}
        queued = waiting = 0
        why = ""
        now = time.time()
        for pack, by_idx in out.items():
            info = self.vault.pack_info(pack)
            ss = info["ss"] if info else 0
            holders = self.holders_of(pack)
            count = {m: len(holders.get(m, [])) for m in live}
            for (p, i), (m, ts) in list(self.pending.items()):
                if p == pack and m in count:
                    count[m] += 1
            load = {m: 0 for m in live}
            for idx, path in sorted(by_idx.items()):
                pend = self.pending.get((pack, idx))
                if pend and now - pend[1] < RESEND_AFTER:
                    continue
                refused = self.refused.get((pack, idx), (None,))[0]
                cands = [m for m in live if live[m].get("free", 0) >= ss + 4096 - load[m] and m != refused
                         and idx not in holders.get(m, [])]
                if not cands:
                    waiting += 1
                    why = "no team member has room for a shard" if live else "no team member keeps shards right now"
                    continue
                m = min(cands, key=lambda c: (count[c], load[c], -live[c].get("free", 0)))
                with open(path, "rb") as f:
                    blob = f.read()
                self.send_blob(m, "shard", {"pack": pack, "idx": idx}, blob)
                with self._lock:
                    self.pending[(pack, idx)] = (m, now)
                count[m] += 1
                load[m] += len(blob)
                queued += 1
        return {"queued": queued, "waiting": waiting, "why": why}

    # ---- owner: gathering -------------------------------------------------------------------------------------
    def ask(self, packs):
        """Ask every member for its shards of these packs (a request signed with the vault key). -> request id"""
        req = self.vault.signed("ask", {"packs": sorted(packs), "reply_to": self.me})
        rid = req["b"]["nonce"]
        with self._lock:
            self.gathered[rid] = {}
        members = [m for m in self.live_members()]
        items = [{"to": m, "kind": "ask", "meta": {"req": req}, "data": ""} for m in members]
        if items:
            self._call("vault_send", {"items": items})
        return rid

    def found(self, rid):
        with self._lock:
            return {p: dict(v) for p, v in (self.gathered.get(rid) or {}).items()}

    def gather(self, packs, need=None, timeout=60, local=True):
        """Shards of these packs from the room (and, when ``local``, still waiting here): returns as soon as each pack
        has ``need[pack]`` shards (default: its k), or at the timeout with what came. -> {pack: {idx: …}}"""
        self.refresh()
        rid = self.ask(packs)
        found = self.gathered[rid]
        if local:
            for pack, by_idx in self.vault.outbox().items():
                if pack in packs:
                    for idx, path in by_idx.items():
                        try:
                            with open(path, "rb") as f:
                                core.add_shard(found, f.read(), owner=self.vault.pub)
                        except (OSError, core.VaultError):
                            pass
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                ok = all(len(found.get(p, {})) >= (need or {}).get(p, self._k_of(p, found)) for p in packs)
            if ok:
                break
            if not self.running:
                self.step()
            time.sleep(0.05)
        with self._lock:
            self.gathered.pop(rid, None)
        return {p: dict(v) for p, v in found.items()}

    def _k_of(self, pack, found):
        info = self.vault.pack_info(pack)
        if info:
            return info["k"]
        got = found.get(pack)
        return int(next(iter(got.values()))[0]["k"]) if got else 10 ** 6

    # ---- owner: health and repair ----------------------------------------------------------------------------
    def health(self):
        """Per snapshot: shards alive (by the members' own reports), how many computers it can lose, its state."""
        self.refresh()
        out = []
        for s in self.vault.snapshots():
            packs = self.vault.packs_of(s["id"])
            worst, alive_min, n_of = None, None, None
            per = []
            for p in packs:
                info = self.vault.pack_info(p) or {}
                k, n = info.get("k", core.DEFAULT_K), info.get("n", core.DEFAULT_N)
                h = self.holders_of(p)
                idx = set(i for v in h.values() for i in v)
                waiting = len(self.vault.outbox().get(p, {}))
                lose = core.survives({m: len(v) for m, v in h.items()}, k)
                per.append({"pack": p, "k": k, "n": n, "alive": len(idx), "holders": len(h), "can_lose": lose,
                            "waiting": waiting})
                worst = lose if worst is None else min(worst, lose)
                alive_min = len(idx) if alive_min is None else min(alive_min, len(idx))
                n_of = n
            state = "safe" if worst is not None and worst >= 1 else ("at risk" if worst == 0 else "lost")
            if worst is not None and worst < 0 and any(x["waiting"] for x in per):
                state = "waiting"
            out.append(dict(s, packs=per, alive=alive_min or 0, n=n_of, can_lose=worst, state=state))
        return out

    def repair(self, timeout=90):
        """Every pack of mine short of shards: gather k, rebuild the lost ones (the very bytes first made) and place
        them on members that keep none of that pack. -> {"repaired": n, "short": [packs that cannot be repaired]}"""
        self.refresh()
        known = dict(self.vault.db.execute("SELECT id, n FROM pack").fetchall())
        unknown = [p for p in self.my_packs_in_room() if p not in known]   # a new PC: learn them from the team
        todo = {}
        for pack, n in known.items():
            h = self.holders_of(pack)
            alive = set(i for v in h.values() for i in v) | set(self.vault.outbox().get(pack, {}))
            missing = [i for i in range(n) if i not in alive]
            if missing:
                todo[pack] = missing
        if not todo and not unknown:
            return {"repaired": 0, "short": []}
        found = self.gather(list(todo) + unknown, timeout=timeout, local=True)
        for pack in unknown:
            got = found.get(pack)
            if got:
                e = next(iter(got.values()))
                self.vault.adopt_pack(e[0], e[3])
                h = self.holders_of(pack)
                alive = set(i for v in h.values() for i in v)
                missing = [i for i in range(int(e[0]["n"])) if i not in alive]
                if missing:
                    todo[pack] = missing
        repaired, short = 0, []
        for pack, missing in todo.items():
            got = found.get(pack, {})
            info = self.vault.pack_info(pack)
            if not info or len(got) < info["k"]:
                short.append(pack)
                continue
            new = core.rebuild_shards(got, missing)
            d = os.path.join(self.vault.root, "own", "out", pack)
            os.makedirs(d, exist_ok=True)
            for i, blob in new.items():
                with open(os.path.join(d, f"{i}.bvs"), "wb") as f:
                    f.write(blob)
                repaired += 1
            for m in list(self.holders_of(pack, live_only=False)):
                if m not in self.live_members():
                    self.vault.forget(m, pack)
        self.place()
        return {"repaired": repaired, "short": short}

    def withdraw(self, snap_id):
        """Remove a snapshot: its packs that no other snapshot needs are withdrawn from the team. -> those packs"""
        gone = self.vault.remove_snapshot(snap_id)
        if gone:
            self.drop(gone)
        return gone

    def drop(self, packs):
        req = self.vault.signed("drop", {"packs": sorted(packs)})
        items = [{"to": m, "kind": "drop", "meta": {"req": req}, "data": ""} for m in self.live_members()]
        if items:
            self._call("vault_send", {"items": items})

    def keep(self, packs):
        """Cancel the pending deletion of these packs on every member (a signed keep)."""
        req = self.vault.signed("keep", {"packs": sorted(packs)})
        items = [{"to": m, "kind": "keep", "meta": {"req": req}, "data": ""} for m in self.live_members()]
        if items:
            self._call("vault_send", {"items": items})

    def dropping(self):
        """{pack: earliest deletion time} of MY packs that members are going to delete (after a drop)."""
        out = {}
        for mid, v in ((self.status or {}).get("members") or {}).items():
            for p, e in (v.get("inv") or {}).items():
                if e.get("o") == self.vault.pub and e.get("d"):
                    out[p] = min(out.get(p, e["d"]), e["d"])
        return out

    def wait_idle(self, timeout=60):
        """Until nothing waits to be sent or acknowledged (tests, the dialog)."""
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                idle = not self.out and not self.pending
            if idle:
                return True
            time.sleep(0.05)
        return False


# ---- links to the board -------------------------------------------------------------------------------------------
class HostLink:
    """The board on this very computer (the room's host): no message."""

    def __init__(self, hub):
        self.hub = hub

    def call(self, path, obj):
        status, out = board_of(self.hub).route(path, obj, self.hub.me)
        if status != 200:
            raise LinkError(out.get("error", "refused"))
        return out


class MemberLink:
    """The board on the host, through the room's sealed messages."""

    def __init__(self, node):
        self.node = node

    def call(self, path, obj):
        n = self.node
        if n.client is None or not n.member:
            raise LinkError("not connected")
        return n.client.call(path, dict(obj, token=n.member["token"]), timeout=60)


def link_of(node):
    if node is None:
        return None
    if node.role == "host" and node.hub is not None:
        return HostLink(node.hub)
    if node.role == "member":
        return MemberLink(node)
    return None


# ---- the keeper of this computer ----------------------------------------------------------------------------------
_KEEPER = [None]
SETTINGS = "biruni/team_vault/"


def load_settings():
    try:
        from qgis.core import QgsSettings

        s = QgsSettings()
        return (s.value(SETTINGS + "hold", True, type=bool),
                int(s.value(SETTINGS + "quota_mb", 1024, type=int)))
    except Exception:  # noqa: BLE001
        return True, 1024


def save_settings(hold, quota_mb):
    from qgis.core import QgsSettings

    s = QgsSettings()
    s.setValue(SETTINGS + "hold", bool(hold))
    s.setValue(SETTINGS + "quota_mb", int(quota_mb))
    k = _KEEPER[0]
    if k is not None:
        k.hold = bool(hold)
        k.vault.quota = int(quota_mb) << 20
        k._last_have = 0


def current():
    return _KEEPER[0]


def on_room(node):
    """A room was opened, joined or left: this computer's keeper follows it."""
    k = _KEEPER[0]
    if node is None:
        if k is not None:
            k.stop(wait=0)
            _KEEPER[0] = None
        return
    try:
        hold, quota = load_settings()
        if k is None:
            v = core.Vault()
            v.quota = quota << 20
            name = (getattr(node, "ident", None) or {}).get("name", "")
            k = Keeper(v, lambda: link_of(node), name=name, hold=hold, tick=1.0)
            _KEEPER[0] = k
        else:
            k.link_fn = lambda: link_of(node)
        k.start()
    except Exception:  # noqa: BLE001 - never stop a room from opening
        pass


def held_cleanup(vault):
    """Remove pieces of transfers that never finished (none are kept on disk: assembly is in memory)."""
    d = os.path.join(vault.held_dir(), ".incoming")
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)


def to_json(x):
    return json.dumps(x, ensure_ascii=False)
