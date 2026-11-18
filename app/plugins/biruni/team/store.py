"""The room's record on each computer: one SQLite file, the same on the host and on members.

* ops        - every change in the order the host accepted it (the stream): who, when, which
               feature, the state after and the state before (so any feature can be taken back
               to any moment), and its signature. Members keep a full copy, which is what lets
               any of them take over as host.
* versions   - the version of every feature (how many changes it has had).
* outbox     - changes this computer made that are not in the stream yet; they are sent again
               after a lost connection or a change of host, so nothing is lost.
* conflicts  - changes that met someone else's change and were not joined, with both versions.
* meta       - the room's settings: name, layers, this computer's member id and fid block.
* members    - (host) who is in the room, their public key and rights.
"""
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ops (seq INTEGER PRIMARY KEY, uid TEXT UNIQUE, epoch INTEGER, author TEXT,
    name TEXT, ts REAL, layer TEXT, fid INTEGER, kind TEXT, state TEXT, prev TEXT, ver INTEGER, sig TEXT,
    note TEXT);
CREATE INDEX IF NOT EXISTS ops_feature ON ops (layer, fid);
CREATE TABLE IF NOT EXISTS versions (layer TEXT, fid INTEGER, ver INTEGER, PRIMARY KEY (layer, fid));
CREATE TABLE IF NOT EXISTS outbox (uid TEXT PRIMARY KEY, ts REAL, body TEXT);
CREATE TABLE IF NOT EXISTS conflicts (id INTEGER PRIMARY KEY, uid TEXT, layer TEXT, fid INTEGER, author TEXT,
    name TEXT, other TEXT, ts REAL, data TEXT, resolved INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS members (id TEXT PRIMARY KEY, name TEXT, pub TEXT, rank INTEGER, rights TEXT,
    token TEXT, joined REAL, seen REAL, state TEXT);
CREATE TABLE IF NOT EXISTS lww (layer TEXT, fid INTEGER, reg TEXT, l INTEGER, c INTEGER, author TEXT,
    uid TEXT, PRIMARY KEY (layer, fid, reg));
CREATE TABLE IF NOT EXISTS lww_dead (layer TEXT, fid INTEGER, state TEXT, PRIMARY KEY (layer, fid));
"""
OP_COLS = ("seq", "uid", "epoch", "author", "name", "ts", "layer", "fid", "kind", "state", "prev", "ver", "sig", "note",
           "src", "lww")
JSON_COLS = ("state", "prev", "src", "lww")


class Store:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.RLock()
        self.con = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=FULL")
        self.con.executescript(SCHEMA)
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(ops)")}
        if "src" not in cols:   # a room made before: what was on the screen when a change was made (provenance)
            self.con.execute("ALTER TABLE ops ADD COLUMN src TEXT")
        if "lww" not in cols:   # HLC and register set for direct peer apply
            self.con.execute("ALTER TABLE ops ADD COLUMN lww TEXT")

    def close(self):
        with self._lock:
            try:
                self.con.close()
            except sqlite3.Error:
                pass

    # ---- meta -------------------------------------------------------------------------------------
    def get(self, k, default=None):
        with self._lock:
            row = self.con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, k, v):
        with self._lock:
            self.con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, json.dumps(v)))

    # ---- the stream -------------------------------------------------------------------------------
    def head(self):
        with self._lock:
            return self.con.execute("SELECT COALESCE(MAX(seq), 0) FROM ops").fetchone()[0]

    def has(self, uid):
        with self._lock:
            return self.con.execute("SELECT 1 FROM ops WHERE uid=?", (uid,)).fetchone() is not None

    def missing(self, uids):
        with self._lock:
            return [u for u in uids if self.con.execute("SELECT 1 FROM ops WHERE uid=?", (u,)).fetchone() is None]

    def append(self, op):
        """Keep one stream entry (op["seq"] set by the host). Returns False for one already kept."""
        cols = OP_COLS[1:]  # seq: this computer's own order (on the host it is the stream's sequence number)
        row = [op.get(c) for c in cols]
        for i in (cols.index(c) for c in JSON_COLS):
            row[i] = json.dumps(row[i]) if row[i] is not None else None
        with self._lock:
            try:
                cur = self.con.execute(f"INSERT INTO ops ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                       row)
            except sqlite3.IntegrityError:
                return False
            op["local_seq"] = cur.lastrowid
            self.con.execute("INSERT OR REPLACE INTO versions (layer, fid, ver) VALUES (?, ?, ?)",
                             (op["layer"], op["fid"], op["ver"]))
            self.con.execute("DELETE FROM outbox WHERE uid=?", (op["uid"],))
        return True

    def append_many(self, ops):
        """Keep many stream entries in one transaction (one write to the disk). [kept?] in order."""
        cols = OP_COLS[1:]
        out = []
        with self._lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                for op in ops:
                    row = [op.get(c) for c in cols]
                    for i in (cols.index(c) for c in JSON_COLS):
                        row[i] = json.dumps(row[i]) if row[i] is not None else None
                    try:
                        cur = self.con.execute(
                            f"INSERT INTO ops ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", row)
                    except sqlite3.IntegrityError:
                        out.append(False)
                        continue
                    op["local_seq"] = cur.lastrowid
                    self.con.execute("INSERT OR REPLACE INTO versions (layer, fid, ver) VALUES (?, ?, ?)",
                                     (op["layer"], op["fid"], op["ver"]))
                    self.con.execute("DELETE FROM outbox WHERE uid=?", (op["uid"],))
                    out.append(True)
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise
        return out

    def _rows(self, sql, args):
        with self._lock:
            rows = self.con.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(zip(OP_COLS, r))
            for k in JSON_COLS:
                d[k] = json.loads(d[k]) if d[k] else None
            if d["src"] is None:
                del d["src"]
            if d["lww"] is None:
                del d["lww"]
            out.append(d)
        return out

    def since(self, seq, limit=500):
        return self._rows(f"SELECT {', '.join(OP_COLS)} FROM ops WHERE seq>? ORDER BY seq LIMIT ?", (seq, limit))

    def last(self, n):
        return list(reversed(self._rows(f"SELECT {', '.join(OP_COLS)} FROM ops ORDER BY seq DESC LIMIT ?", (n,))))

    def feature_history(self, layer, fid):
        return self._rows(f"SELECT {', '.join(OP_COLS)} FROM ops WHERE layer=? AND fid=? ORDER BY seq", (layer, fid))

    def last_by(self, author, n=1):
        return self._rows(f"SELECT {', '.join(OP_COLS)} FROM ops WHERE author=? AND note IS NOT 'undo' "
                          "ORDER BY seq DESC LIMIT ?", (author, n))

    def by_author_since(self, author, ts):
        """A member's changes from time ``ts`` on, in the stream's order (to take back a member's work)."""
        return self._rows(f"SELECT {', '.join(OP_COLS)} FROM ops WHERE author=? AND ts>=? AND kind!='field' "
                          "ORDER BY seq", (author, ts))

    def version(self, layer, fid):
        with self._lock:
            row = self.con.execute("SELECT ver FROM versions WHERE layer=? AND fid=?", (layer, fid)).fetchone()
        return row[0] if row else 0

    def set_versions(self, items):
        with self._lock:
            self.con.execute("BEGIN")
            self.con.executemany("INSERT OR REPLACE INTO versions (layer, fid, ver) VALUES (?, ?, ?)", items)
            self.con.execute("COMMIT")

    def all_versions(self):
        with self._lock:
            return self.con.execute("SELECT layer, fid, ver FROM versions").fetchall()

    def counts(self):
        with self._lock:
            return dict(self.con.execute("SELECT author, COUNT(*) FROM ops GROUP BY author").fetchall())

    # ---- this computer's changes not in the stream yet -----------------------------------------------
    def queue(self, op):
        with self._lock:
            self.con.execute("INSERT OR REPLACE INTO outbox (uid, ts, body) VALUES (?, ?, ?)",
                             (op["uid"], time.time(), json.dumps(op)))

    def outbox(self, limit=200):
        with self._lock:
            # in the order they were made (the row order): a computer's clock set back must not send an older
            # state after a newer one
            rows = self.con.execute("SELECT body FROM outbox ORDER BY rowid LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def outbox_op(self, uid):
        with self._lock:
            r = self.con.execute("SELECT body FROM outbox WHERE uid=?", (uid,)).fetchone()
        return json.loads(r[0]) if r else None

    def drop_outbox(self, uid):
        with self._lock:
            self.con.execute("DELETE FROM outbox WHERE uid=?", (uid,))

    def outbox_count(self):
        with self._lock:
            return self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    # ---- conflicts ------------------------------------------------------------------------------------
    def add_conflict(self, c):
        with self._lock:
            cur = self.con.execute(
                "INSERT INTO conflicts (uid, layer, fid, author, name, other, ts, data) VALUES (?,?,?,?,?,?,?,?)",
                (c["uid"], c["layer"], c["fid"], c.get("author"), c.get("name"), c.get("other"), time.time(),
                 json.dumps(c)))
            return cur.lastrowid

    def open_conflicts(self):
        with self._lock:
            rows = self.con.execute("SELECT id, data FROM conflicts WHERE resolved=0 ORDER BY id").fetchall()
        return [dict(json.loads(d), id=i) for i, d in rows]

    def resolve_conflict(self, cid):
        with self._lock:
            self.con.execute("UPDATE conflicts SET resolved=1 WHERE id=?", (cid,))

    # ---- members (host) -------------------------------------------------------------------------------
    def members(self):
        with self._lock:
            rows = self.con.execute("SELECT id, name, pub, rank, rights, token, joined, seen, state FROM members "
                                    "ORDER BY rank").fetchall()
        return [{"id": r[0], "name": r[1], "pub": r[2], "rank": r[3], "rights": json.loads(r[4] or "{}"),
                 "token": r[5], "joined": r[6], "seen": r[7], "state": r[8]} for r in rows]

    def save_member(self, m):
        with self._lock:
            self.con.execute("INSERT OR REPLACE INTO members (id, name, pub, rank, rights, token, joined, seen, state) "
                             "VALUES (?,?,?,?,?,?,?,?,?)",
                             (m["id"], m["name"], m["pub"], m["rank"], json.dumps(m.get("rights") or {}),
                              m.get("token"), m.get("joined"), m.get("seen"), m.get("state")))

    def replace_members(self, members):
        with self._lock:
            self.con.execute("BEGIN")
            self.con.execute("DELETE FROM members")
            for m in members:
                self.con.execute("INSERT INTO members (id, name, pub, rank, rights, token, joined, seen, state) "
                                 "VALUES (?,?,?,?,?,?,?,?,?)",
                                 (m["id"], m["name"], m.get("pub"), m["rank"], json.dumps(m.get("rights") or {}),
                                  m.get("token"), m.get("joined"), m.get("seen"), m.get("state")))
            self.con.execute("COMMIT")

    # ---- LWW registers (direct peer apply) ------------------------------------------------------------
    def regs(self, layer, fid):
        """{reg: (l, c, author, uid)} for one feature."""
        with self._lock:
            rows = self.con.execute(
                "SELECT reg, l, c, author, uid FROM lww WHERE layer=? AND fid=?", (layer, int(fid))
            ).fetchall()
        return {r[0]: (int(r[1]), int(r[2]), r[3], r[4]) for r in rows}

    def put_regs(self, layer, fid, regs):
        fid = int(fid)
        with self._lock:
            self.con.execute("BEGIN")
            try:
                self.con.execute("DELETE FROM lww WHERE layer=? AND fid=?", (layer, fid))
                if regs:
                    self.con.executemany(
                        "INSERT INTO lww (layer, fid, reg, l, c, author, uid) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(layer, fid, reg, int(s[0]), int(s[1]), s[2], s[3]) for reg, s in regs.items()],
                    )
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise

    def dead(self, layer, fid):
        """Last known state dict of a dead feature, or None."""
        with self._lock:
            row = self.con.execute(
                "SELECT state FROM lww_dead WHERE layer=? AND fid=?", (layer, int(fid))
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def put_dead(self, layer, fid, state):
        fid = int(fid)
        with self._lock:
            if state is None:
                self.con.execute("DELETE FROM lww_dead WHERE layer=? AND fid=?", (layer, fid))
            else:
                self.con.execute(
                    "INSERT OR REPLACE INTO lww_dead (layer, fid, state) VALUES (?, ?, ?)",
                    (layer, fid, json.dumps(state)),
                )

    def save_lww(self, items):
        """Save many (layer, fid, regs, dead) updates in one transaction."""
        if not items:
            return
        with self._lock:
            self.con.execute("BEGIN")
            try:
                for layer, fid, regs, dead in items:
                    fid = int(fid)
                    self.con.execute("DELETE FROM lww WHERE layer=? AND fid=?", (layer, fid))
                    if regs:
                        self.con.executemany(
                            "INSERT INTO lww (layer, fid, reg, l, c, author, uid) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [(layer, fid, reg, int(s[0]), int(s[1]), s[2], s[3]) for reg, s in regs.items()],
                        )
                    if dead is None:
                        self.con.execute("DELETE FROM lww_dead WHERE layer=? AND fid=?", (layer, fid))
                    else:
                        self.con.execute(
                            "INSERT OR REPLACE INTO lww_dead (layer, fid, state) VALUES (?, ?, ?)",
                            (layer, fid, json.dumps(dead)),
                        )
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise

    def lww_dump(self):
        """JSON-serializable snapshot of lww and lww_dead tables."""
        with self._lock:
            regs = self.con.execute("SELECT layer, fid, reg, l, c, author, uid FROM lww").fetchall()
            dead = self.con.execute("SELECT layer, fid, state FROM lww_dead").fetchall()
        return {
            "regs": [list(r) for r in regs],
            "dead": [[r[0], r[1], json.loads(r[2]) if r[2] else None] for r in dead],
        }

    def lww_load(self, d):
        """Replace lww and lww_dead tables from a dump dict."""
        d = d or {}
        regs = d.get("regs") or []
        dead = d.get("dead") or []
        with self._lock:
            self.con.execute("BEGIN")
            try:
                self.con.execute("DELETE FROM lww")
                self.con.execute("DELETE FROM lww_dead")
                if regs:
                    self.con.executemany(
                        "INSERT INTO lww (layer, fid, reg, l, c, author, uid) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(r[0], int(r[1]), r[2], int(r[3]), int(r[4]), r[5], r[6]) for r in regs],
                    )
                if dead:
                    self.con.executemany(
                        "INSERT INTO lww_dead (layer, fid, state) VALUES (?, ?, ?)",
                        [(r[0], int(r[1]), json.dumps(r[2])) for r in dead if r[2] is not None],
                    )
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise
