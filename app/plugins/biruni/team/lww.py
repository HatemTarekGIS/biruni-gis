"""Last-writer-wins (LWW) per-register state for direct peer apply (no Qt).

Registers per feature:
- "g"         whole geometry (WKB hex)
- "a:<field>" one attribute field
- "del"       tombstone
- "*"         whole-feature write; fallback stamp for "g" and "a:*" that have no row of their own.

Stamps are (l, c, author, uid) tuples compared lexicographically; missing registers count as ZERO.
A feature is alive iff there is no tombstone, or max(non-del stamps) > tombstone. When dead, its last
field/geometry values stay in dead state so an edit newer than the delete brings it back with all
its last values.

A feature this computer never had (live is None and dead is None) with a set without "*" must wait
at the caller and never be applied here.

Limits:
- A correction is stamped just after the refused op, so an older in-flight legitimate edit of the
  same field loses to it.
- A clock that is behind loses until that computer reads a teammate's edit, which lifts it through
  the HLC.
- A removed member's undelivered work is dropped, as today.
- Computers on an older version follow the host only.
"""

ZERO = (0, 0, "", "")


def _copy_state(st):
    if st is None:
        return None
    return {"g": st.get("g"), "a": dict(st.get("a") or {})}


def stamp_of(x, hlc=None):
    """Build a comparable (l, c, author, uid) tuple from an op/entry."""
    if hlc is None:
        hlc, _ = delta(x)
    return (int(hlc[0]), int(hlc[1]), str(x.get("author") or ""), str(x.get("uid") or ""))


def eff(regs, reg):
    """Effective stamp of ``reg`` in ``regs`` (falling back to "*" for g/a:*)."""
    if reg in regs:
        return regs[reg]
    if reg == "g" or reg.startswith("a:"):
        return regs.get("*", ZERO)
    return ZERO


def make_set(kind, base, state):
    """Return the register set touched by an op with ``kind``, ``base``, and ``state``."""
    if kind == "del" or state is None:
        return ["del"]
    if base is None:
        return ["*"]
    out = []
    if state.get("g") != base.get("g"):
        out.append("g")
    ba = base.get("a") or {}
    sa = state.get("a") or {}
    for f in sorted(set(ba) | set(sa)):
        if sa.get(f) != ba.get(f):
            out.append("a:" + f)
    return out


def delta(x):
    """Return (hlc, reg_set) for an op or stream entry, synthesizing from ts/base if pre-switch."""
    lww = x.get("lww")
    if isinstance(lww, dict) and lww.get("hlc") is not None and lww.get("set") is not None:
        hlc = lww["hlc"]
        return [int(hlc[0]), int(hlc[1])], list(lww["set"])
    if x.get("hlc") is not None and x.get("set") is not None:
        hlc = x["hlc"]
        return [int(hlc[0]), int(hlc[1])], list(x["set"])
    ts = float(x.get("ts") or 0)
    base = x.get("base") if x.get("base") is not None else x.get("prev")
    return [int(ts * 1000), 0], make_set(x.get("kind"), base, x.get("state"))


def overlay(cur, op):
    """Return ``cur`` with every register in ``op``'s set written (input to rules.check_op)."""
    if op.get("kind") == "del":
        return None
    st = op.get("state") or {}
    _, reg_set = delta(op)
    if cur is None or "*" in reg_set:
        return _copy_state(st)
    res = _copy_state(cur)
    sa = st.get("a") or {}
    for r in reg_set:
        if r == "g":
            res["g"] = st.get("g")
        elif r.startswith("a:"):
            f = r[2:]
            if f in sa:
                res["a"][f] = sa[f]
            else:
                res["a"].pop(f, None)
    return res


def apply(regs, live, dead, x):
    """Apply op/entry ``x`` to ``(regs, live, dead)`` -> ``(live2, regs2, dead2, changed)``."""
    regs = regs or {}
    regs2 = dict(regs)
    hlc, reg_set = delta(x)
    stamp = stamp_of(x, hlc)
    base_s = live if live is not None else dead
    s = _copy_state(base_s)

    if "del" in reg_set or x.get("kind") == "del":
        if stamp > regs2.get("del", ZERO):
            regs2["del"] = stamp
    else:
        op_state = x.get("state") or {}
        op_a = op_state.get("a") or {}
        if "*" in reg_set:
            if stamp > eff(regs2, "g"):
                if s is None:
                    s = {"g": None, "a": {}}
                s["g"] = op_state.get("g")
            all_fields = set(op_a) | (set(s["a"]) if s is not None else set())
            for f in sorted(all_fields):
                if stamp > eff(regs2, "a:" + f):
                    if s is None:
                        s = {"g": None, "a": {}}
                    if f in op_a:
                        s["a"][f] = op_a[f]
                    else:
                        s["a"].pop(f, None)
            for r in list(regs2):
                if r not in ("*", "del") and regs2[r] < stamp:
                    del regs2[r]
            if stamp > regs2.get("*", ZERO):
                regs2["*"] = stamp
        else:
            for r in reg_set:
                if r in ("*", "del"):
                    continue
                if stamp > eff(regs2, r):
                    if s is None:
                        s = {"g": None, "a": {}}
                    if r == "g":
                        s["g"] = op_state.get("g")
                    elif r.startswith("a:"):
                        f = r[2:]
                        if f in op_a:
                            s["a"][f] = op_a[f]
                        else:
                            s["a"].pop(f, None)
                    regs2[r] = stamp

    tomb = regs2.get("del")
    others = [v for k, v in regs2.items() if k != "del"]
    if s is None:
        alive = False
    elif tomb is None:
        alive = True
    else:
        alive = bool(others) and max(others) > tomb
    live2 = s if alive else None
    dead2 = None if alive else s
    changed = (live2 != live) or (dead2 != dead) or (regs2 != regs)
    return live2, regs2, dead2, changed
