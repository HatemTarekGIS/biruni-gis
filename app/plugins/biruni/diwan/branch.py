"""Branches of a Diwan file: a copy that goes away (a field laptop, a colleague) and is synchronised with the file
it came from, both ways at once (SPEC.md, "Branches and synchronising").

* make_branch(main, copy, name): the copy remembers where it came from; the main file lists its branches.
* sync(main, branch): what changed on either side since the last sync goes to the other side. A feature both
  sides changed is joined field by field and vertex by vertex (team/merge.py - the team room's merge): one side
  moves the north corner, the other the south one, and both moves are kept. Where both changed the same thing
  differently, the main file's value is kept on both sides and the conflict is listed in the main file, to be
  turned around later (resolve). After a sync the two files hold the same features.

The joining uses the engine's geometry (team/merge.py), so this module - unlike store.py - needs it.
"""
import json
import sqlite3
import uuid

from .store import Diwan, DiwanError, now, state_hash, wkb_of

SCHEMA = """
CREATE TABLE IF NOT EXISTS diwan_branches (
  branch_id TEXT PRIMARY KEY, name TEXT, created TEXT NOT NULL,
  main_synced INTEGER NOT NULL, branch_synced INTEGER NOT NULL, last_sync TEXT);
CREATE TABLE IF NOT EXISTS diwan_conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, uid TEXT NOT NULL, at TEXT NOT NULL,
  branch TEXT, what TEXT NOT NULL, field TEXT, kept TEXT, other TEXT, resolved INTEGER NOT NULL DEFAULT 0);
"""


def _max_seq(d):
    return d.con.execute("SELECT COALESCE(MAX(seq), 0) FROM diwan_log").fetchone()[0]


def _set_meta(d, key, value):
    d.con.execute("INSERT OR REPLACE INTO diwan_meta VALUES (?, ?)", (key, str(value)))


def make_branch(main_path, copy_path, name):
    """Copy a Diwan file as a branch named ``name``. -> the branch id."""
    with Diwan(main_path) as m:
        if m.meta("format") != "diwan":
            raise DiwanError("Turn on the Diwan for this file first.")
        if m.meta("role") == "branch":
            raise DiwanError("This file is itself a branch: make branches of the main file.")
        with m.con:
            m.con.executescript(SCHEMA)
        dst = sqlite3.connect(copy_path)
        try:
            m.con.backup(dst)   # a consistent copy even while the file is open elsewhere
        finally:
            dst.close()
        top = _max_seq(m)
        bid = str(uuid.uuid4())
        with m.con:
            m.con.execute("INSERT INTO diwan_branches VALUES (?, ?, ?, ?, ?, NULL)", (bid, name, now(), top, top))
        main_id = m.meta("file_id")
    with Diwan(copy_path) as b:
        with b.con:
            b.con.execute("DELETE FROM diwan_branches")
            b.con.execute("DELETE FROM diwan_conflicts")
            for k, v in (("role", "branch"), ("branch_id", bid), ("branch_name", name), ("main_file_id", main_id),
                         ("file_id", str(uuid.uuid4())), ("main_synced", top), ("branch_synced", top),
                         ("main_path", main_path)):
                _set_meta(b, k, v)
    return bid


def branches(main_path):
    """[{branch_id, name, created, last_sync}] of a main file."""
    with Diwan(main_path) as m:
        try:
            rows = m.con.execute("SELECT branch_id, name, created, last_sync FROM diwan_branches ORDER BY created")
            return [dict(zip(("branch_id", "name", "created", "last_sync"), r)) for r in rows]
        except sqlite3.Error:
            return []


def conflicts(main_path, open_only=True):
    with Diwan(main_path) as m:
        try:
            sql = "SELECT id, table_name, uid, at, branch, what, field, kept, other, resolved FROM diwan_conflicts"
            rows = m.con.execute(sql + (" WHERE resolved = 0" if open_only else "") + " ORDER BY id")
        except sqlite3.Error:
            return []
        keys = ("id", "table", "uid", "at", "branch", "what", "field", "kept", "other", "resolved")
        return [dict(zip(keys, r)) for r in rows]


# ---- states as the merge sees them: {"g": WKB hex or None, "a": {field: value}} ------------------------
def _state(d, table, uid):
    """The current state of ``uid`` in a file, or None when it has no row."""
    fid = d.fid_of(table, uid)
    if fid is None:
        return None
    for _fid, g, a in d.rows(table, [fid]):
        w = wkb_of(g)
        return {"g": w.hex() if w else None, "a": a}
    return None


def _base(d, table, uid, upto):
    """The version of ``uid`` logged at or before seq ``upto`` (None when it did not exist then)."""
    r = d.con.execute("SELECT op, geom, attrs FROM diwan_log WHERE table_name=? AND uid=? AND seq<=? "
                      "ORDER BY seq DESC LIMIT 1", (table, uid, upto)).fetchone()
    if r is None or r[0] == "delete":
        return None
    w = wkb_of(r[1])
    return {"g": w.hex() if w else None, "a": json.loads(r[2]) if r[2] else {}}


def _key(st):
    if st is None:
        return None
    return state_hash(bytes.fromhex(st["g"]) if st["g"] else b"", st["a"])


def _changed(d, table, since):
    return {r[0] for r in d.con.execute("SELECT DISTINCT uid FROM diwan_log WHERE table_name=? AND seq>?",
                                        (table, since))}


# ---- writing states into a file ----------------------------------------------------------------------------
def _apply(d, table, plan, who, how):
    """Write {uid: state or None} into ``table`` of file ``d`` (through GDAL) and log each as a version."""
    if not plan:
        return 0
    from osgeo import ogr

    ds = d._ogr()
    lyr = ds.GetLayerByName(table)
    defn = lyr.GetLayerDefn()
    done = []
    ds.StartTransaction()
    try:
        for uid, st in plan.items():
            fid = d.fid_of(table, uid)
            if st is None:
                if fid is not None:   # already gone here: nothing to write or log
                    lyr.DeleteFeature(fid)
                    done.append((uid, None))
                continue
            feat = lyr.GetFeature(fid) if fid is not None else None
            new = feat is None
            if new:
                feat = ogr.Feature(defn)
            attrs = {k: v for k, v in st["a"].items() if defn.GetFieldIndex(k) >= 0}
            d._fill(feat, defn, bytes.fromhex(st["g"]) if st["g"] else b"", attrs)
            if new:
                lyr.CreateFeature(feat)
            else:
                lyr.SetFeature(feat)
            done.append((uid, feat.GetFID()))
        ds.CommitTransaction()
    except Exception:
        ds.RollbackTransaction()
        raise
    finally:
        ds = None   # noqa: F841
    at = now()
    cur = d.current(table)
    with d.con:
        for uid, fid in done:
            prev = cur.get(uid, (None,))[0]
            if fid is None:
                d.con.execute("DELETE FROM diwan_ids WHERE table_name=? AND uid=?", (table, uid))
                d._log(table, uid, "delete", at, who, how, None, None, prev)
                continue
            d.con.execute("DELETE FROM diwan_ids WHERE table_name=? AND (uid=? OR fid=?)", (table, uid, fid))
            d.con.execute("INSERT INTO diwan_ids VALUES (?, ?, ?)", (table, fid, uid))
            for _f, g, a in d.rows(table, [fid]):
                d._log(table, uid, "add" if prev is None or cur.get(uid, (0, "delete"))[1] == "delete" else "change",
                       at, who, how, g, a, prev)
    return len(done)


# ---- synchronising ---------------------------------------------------------------------------------------------
def sync(main_path, branch_path, who=""):
    """Both ways: the branch's changes go to the main file and the main file's to the branch.
    -> {"to_main", "to_branch", "joined", "conflicts": [..]}."""
    m, b = Diwan(main_path), Diwan(branch_path)
    try:
        bid = b.meta("branch_id")
        if b.meta("role") != "branch" or b.meta("main_file_id") != m.meta("file_id"):
            raise DiwanError("This file is not a branch of that file.")
        with m.con:
            m.con.executescript(SCHEMA)
        row = m.con.execute("SELECT name, main_synced, branch_synced FROM diwan_branches WHERE branch_id=?",
                            (bid,)).fetchone()
        if row is None:
            raise DiwanError("The main file does not know this branch.")
        name, ms, bs = row
        # edits saved by other programs since: logged first, so they count
        m.reconcile(who="", how="external")
        b.reconcile(who="", how="external")
        report = {"to_main": 0, "to_branch": 0, "joined": 0, "conflicts": []}
        from ..team.merge import merge_feature

        for table in sorted(set(m.tables()) & set(b.tables())):
            in_m, in_b = _changed(m, table, ms), _changed(b, table, bs)
            to_m, to_b = {}, {}
            for uid in sorted(in_m | in_b):
                ms_, bs_ = _state(m, table, uid), _state(b, table, uid)
                if _key(ms_) == _key(bs_):
                    continue
                if uid not in in_b:                     # only the main file changed it
                    to_b[uid] = ms_
                    continue
                if uid not in in_m:                     # only the branch changed it
                    to_m[uid] = bs_
                    continue
                # both changed it
                if ms_ is None or bs_ is None:   # deleted on one side, changed on the other: the change is kept
                    if ms_ is None:
                        to_m[uid] = bs_
                    else:
                        to_b[uid] = ms_
                    report["conflicts"].append({"table": table, "uid": uid, "what": "deleted", "field": "",
                                                "kept": "changed", "other": "deleted"})
                    continue
                base = _base(b, table, uid, bs) or {"g": None, "a": {}}
                merged, confs = merge_feature(base, bs_, ms_)   # the main file's value wins a conflict
                report["joined"] += 1
                if _key(merged) != _key(ms_):
                    to_m[uid] = merged
                if _key(merged) != _key(bs_):
                    to_b[uid] = merged
                for c in confs:
                    report["conflicts"].append({"table": table, "uid": uid, "what": c["what"],
                                                "field": c.get("field", ""), "kept": c.get("theirs"),
                                                "other": c.get("mine")})
            report["to_main"] += _apply(m, table, to_m, who, f"sync:{name}")
            report["to_branch"] += _apply(b, table, to_b, who, "sync:main")
        at = now()
        with m.con:
            for c in report["conflicts"]:
                m.con.execute("INSERT INTO diwan_conflicts (table_name, uid, at, branch, what, field, kept, other) "
                              "VALUES (?,?,?,?,?,?,?,?)", (c["table"], c["uid"], at, name, c["what"], c["field"],
                                                           json.dumps(c["kept"], ensure_ascii=False, default=str),
                                                           json.dumps(c["other"], ensure_ascii=False, default=str)))
            m.con.execute("UPDATE diwan_branches SET main_synced=?, branch_synced=?, last_sync=? WHERE branch_id=?",
                          (_max_seq(m), _max_seq(b), at, bid))
        with b.con:
            _set_meta(b, "main_synced", _max_seq(m))
            _set_meta(b, "branch_synced", _max_seq(b))
        return report
    finally:
        m.close()
        b.close()


def resolve(main_path, conflict_id, use_other, who=""):
    """Close a conflict: keep the main file's value, or (``use_other``) take the branch's - written as a change of
    the main file, so it reaches the branch at the next sync."""
    with Diwan(main_path) as m:
        r = m.con.execute("SELECT table_name, uid, what, field, other FROM diwan_conflicts WHERE id=?",
                          (conflict_id,)).fetchone()
        if r is None:
            raise DiwanError("No such conflict.")
        table, uid, what, field, other = r
        if use_other:
            other = json.loads(other) if other else None
            st = _state(m, table, uid)
            if what == "deleted":
                _apply(m, table, {uid: None}, who, "resolve")
            elif st is not None:
                if what == "field":
                    st["a"][field] = other
                elif what == "geometry":
                    st["g"] = other
                _apply(m, table, {uid: st}, who, "resolve")
        with m.con:
            m.con.execute("UPDATE diwan_conflicts SET resolved=1 WHERE id=?", (conflict_id,))

