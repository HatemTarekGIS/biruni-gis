"""Direct peer apply (LWW + HLC) for team rooms when ``peer_apply`` is on."""
import time

from . import features, lww, rules

SKEW_MS = 300_000
MAX_WAIT = 5000
MAX_REFUSED = 5000


def wall(node):
    """Wall-clock seconds (test seam via ``node.wall``)."""
    fn = getattr(node, "wall", time.time)
    return float(fn())


def _is_future(node, hlc):
    return int(hlc[0]) > int(wall(node) * 1000) + SKEW_MS


def stamp(node, top=lww.ZERO):
    """Advance and persist this computer's HLC above ``now``, stored ``hlc``, and ``top``."""
    now = int(wall(node) * 1000)
    raw = node.store.get("hlc") or [0, 0]
    l0, c0 = int(raw[0]), int(raw[1])
    if l0 > now + SKEW_MS:
        l0, c0 = now, 0
    tl, tc = int(top[0]), int(top[1])
    hlc = list(max((now, 0), (l0, c0 + 1), (tl, tc + 1)))
    node.store.put("hlc", hlc)
    return hlc


def observe(node, hlc):
    """Lift stored HLC to at least ``hlc`` (never called for a held future op)."""
    if not hlc:
        return
    now = int(wall(node) * 1000)
    hl, hc = int(hlc[0]), int(hlc[1])
    if hl > now + SKEW_MS:
        return
    raw = node.store.get("hlc") or [0, 0]
    l0, c0 = int(raw[0]), int(raw[1])
    if l0 > now + SKEW_MS:
        l0, c0 = now, 0
    nl, nc = max((now, 0), (l0, c0), (hl, hc))
    node.store.put("hlc", [nl, nc])


def own(node, op):
    """Apply this computer's own op to its LWW registers (the layer file already has it)."""
    if op.get("kind") == "field" or op.get("hlc") is None:
        return
    key, fid = op["layer"], int(op["fid"])
    regs = node.store.regs(key, fid)
    dead = node.store.dead(key, fid)
    live = op.get("base")
    _live2, regs2, dead2, _changed = lww.apply(regs, live, dead, op)
    node.store.put_regs(key, fid, regs2)
    node.store.put_dead(key, fid, dead2)


def _cap_wait(wait):
    if len(wait) > MAX_WAIT:
        for k in list(wait)[: len(wait) - MAX_WAIT]:
            del wait[k]
    return wait


def _cap_refused(refused):
    if len(refused) > MAX_REFUSED:
        return refused[-MAX_REFUSED:]
    return refused


def add_wait(store, uid, op, maker, why):
    wait = dict(store.get("peer_wait") or {})
    wait[uid] = {"op": op, "maker": maker, "why": why}
    store.put("peer_wait", _cap_wait(wait))


def unwait(store, uids):
    """Drop resolved/confirmed uids (including ``<uid>:x`` corrections) from wait and refused lists."""
    if not uids:
        return
    drop = set()
    for u in uids:
        if u:
            drop.add(u)
            if u.endswith(":x"):
                drop.add(u[:-2])
    wait = store.get("peer_wait")
    if isinstance(wait, dict) and any(u in wait for u in drop):
        wait = {k: v for k, v in wait.items() if k not in drop}
        store.put("peer_wait", wait)
    refused = store.get("peer_refused")
    if isinstance(refused, list) and any(u in drop for u in refused):
        store.put("peer_refused", [u for u in refused if u not in drop])


def sync_status(node, applied=0):
    """Update FolderCarrier status keys ``peer_applied``, ``peer_waiting``, and ``clock_ahead``."""
    from . import folder as rf

    c = rf.carrier_for(node)
    if c is None:
        return
    wait = node.store.get("peer_wait") or {}
    future_n = sum(1 for v in wait.values() if isinstance(v, dict) and v.get("why") == "future")
    if applied:
        c.status["peer_applied"] = int(c.status.get("peer_applied", 0)) + applied
    c.status["peer_waiting"] = future_n
    c.status["clock_ahead"] = future_n > 0


def _delete_guard_hit(node, maker):
    limit = int(node.store.get("delete_limit", 20) or 0)
    if not limit:
        return False
    deletes = getattr(node, "_peer_deletes", None)
    if deletes is None:
        deletes = {}
        node._peer_deletes = deletes
    now = wall(node)
    times = [t for t in deletes.get(maker, []) if now - t < 60]
    if len(times) >= limit:
        deletes[maker] = times
        return True
    times.append(now)
    deletes[maker] = times
    return False


def _roster_entry(node, mid):
    roster = node.roster or node.store.get("roster") or []
    return next((m for m in roster if m.get("id") == mid), None)


def _apply_batch(node, items, rep=None):
    """Run steps 4-8 of peer apply for ``[(op, maker, who, from_retry)]``.

    Returns (applied_count, wait_updates, refused_adds, done_uids).
    """
    from .hub import _src

    st = node.store
    areas = getattr(node, "_areas", None)
    fids_by_layer = {}
    for op, _maker, _who, _r in items:
        key = op.get("layer")
        if key in node.layers:
            fids_by_layer.setdefault(key, set()).add(int(op["fid"]))
    orig_states = {key: features.stored_states(node.layers[key], fids) for key, fids in fids_by_layer.items()}

    live_map, regs_map, dead_map, ver_map = {}, {}, {}, {}
    changes = {}
    lww_dirty = {}
    entries = []
    wait_updates = {}
    refused_adds = []
    done_uids = []
    observed = []

    def _get_state(key, fid):
        k = (key, fid)
        if k not in live_map:
            live_map[k] = orig_states[key].get(fid)
            regs_map[k] = st.regs(key, fid)
            dead_map[k] = st.dead(key, fid)
            ver_map[k] = st.version(key, fid)
        return live_map[k], regs_map[k], dead_map[k]

    for op, maker, who, _from_retry in items:
        uid = str(op.get("uid") or "")
        hlc, reg_set = lww.delta(op)
        if _is_future(node, hlc):
            wait_updates[uid] = {"op": op, "maker": maker, "why": "future"}
            continue
        key = op.get("layer")
        lyr = node.layers.get(key)
        if lyr is None:
            done_uids.append(uid)
            continue
        fid = int(op["fid"])
        if features.is_pending(lyr, fid):
            wait_updates[uid] = {"op": op, "maker": maker, "why": "editing"}
            node.commit_soon(key)
            continue
        live, regs, dead = _get_state(key, fid)
        if live is None and dead is None and "*" not in reg_set:
            wait_updates[uid] = {"op": op, "maker": maker, "why": "unknown"}
            continue
        if op.get("kind") == "del" and _delete_guard_hit(node, maker):
            wait_updates[uid] = {"op": op, "maker": maker, "why": "guard"}
            continue
        ov = lww.overlay(live, op)
        why, checked = rules.check_op(st, node.lookup_member, who, key, op, live, result=ov, layer=lyr, areas=areas)
        if why:
            refused_adds.append(uid)
            done_uids.append(uid)
            if rep is not None:
                rep["refused"].append(why)
            continue
        op_use = dict(op, state=checked) if (op.get("kind") != "del" and checked != ov) else op
        live2, regs2, dead2, changed = lww.apply(regs, live, dead, op_use)
        k = (key, fid)
        live_map[k] = live2
        regs_map[k] = regs2
        dead_map[k] = dead2
        lww_dirty[k] = (key, fid, regs2, dead2)
        ch = changes.setdefault(key, {})
        ch[fid] = (orig_states[key].get(fid), live2)
        observed.append(hlc)
        ver = ver_map[k] + 1
        ver_map[k] = ver
        entry = {
            "seq": None,
            "uid": uid,
            "epoch": node.epoch,
            "author": maker,
            "name": op.get("name") or who.get("name", ""),
            "ts": op.get("ts", time.time()),
            "layer": key,
            "fid": fid,
            "kind": "del" if live2 is None else "put",
            "state": live2,
            "prev": live,
            "ver": ver,
            "sig": op.get("sig"),
            "note": "peer" if changed else "older",
            "src": _src(op.get("src")),
            "lww": {"hlc": hlc, "set": reg_set},
        }
        entries.append(entry)
        done_uids.append(uid)

    for key, ch in changes.items():
        lyr = node.layers[key]
        todo = {fid: (old, new) for fid, (old, new) in ch.items() if old != new}
        if todo and not features.write_states(lyr, todo):
            for fid, (_old, new) in todo.items():
                if new is None:
                    features.delete_stored(lyr, fid)
                else:
                    features.write_state(lyr, fid, new)
        if todo:
            lyr.triggerRepaint()

    if lww_dirty:
        st.save_lww(list(lww_dirty.values()))
    for hlc in observed:
        observe(node, hlc)

    applied = 0
    for entry in entries:
        if st.append(entry):
            node._note(entry)
            applied += 1
    if rep is not None:
        rep["applied"] = rep.get("applied", 0) + applied
    return applied, wait_updates, refused_adds, done_uids


def take(node, body, rep=None):
    """Apply one member's up pack directly on a peer when ``peer_apply`` is on."""
    from .hub import Hub

    st = node.store
    maker = str(body.get("maker") or "")
    if not maker or maker == node.me_id:
        return 0
    who = _roster_entry(node, maker)
    if who is None or who.get("host"):
        return 0

    wait = dict(st.get("peer_wait") or {})
    refused_list = list(st.get("peer_refused") or [])
    refused_set = set(refused_list)
    pub = who.get("pub")

    ready = []
    seen_uids = set()
    for op in body.get("ops") or []:
        if op.get("author") != maker or op.get("replay") or op.get("kind") == "field":
            continue
        if op.get("hlc") is None or op.get("set") is None:
            continue
        uid = str(op.get("uid") or "")
        if not uid or uid in seen_uids or st.has(uid) or uid in wait or uid in refused_set:
            continue
        seen_uids.add(uid)
        if not pub or not Hub._signed(op, pub):
            if rep is not None:
                rep["refused"].append("bad signature")
            continue
        ready.append((dict(op), maker, who, False))

    applied = 0
    if ready:
        applied, wait_updates, refused_adds, _done = _apply_batch(node, ready, rep=rep)
        if wait_updates:
            wait.update(wait_updates)
            st.put("peer_wait", _cap_wait(wait))
        if refused_adds:
            for u in refused_adds:
                if u not in refused_set:
                    refused_list.append(u)
                    refused_set.add(u)
            st.put("peer_refused", _cap_refused(refused_list))
    sync_status(node, applied=applied)
    return applied


def _retry_host(node, wait):
    st = node.store
    hub = node.hub
    results = st.get("pigeon_results") or {}
    res_changed = False
    moved = True
    while moved:
        moved = False
        for uid, item in list(wait.items()):
            if not isinstance(item, dict) or item.get("why") == "guard":
                continue
            op = item.get("op")
            maker = item.get("maker")
            if not op or st.has(uid):
                wait.pop(uid, None)
                moved = True
                continue
            hlc, _ = lww.delta(op)
            if _is_future(node, hlc):
                item["why"] = "future"
                continue
            member = hub._member(maker)
            if member is None:
                wait.pop(uid, None)
                moved = True
                continue
            r = hub.apply_remote(dict(op), member)
            if r == "later":
                node.commit_soon(op.get("layer"))
                item["why"] = "editing"
                continue
            if isinstance(r, dict):
                status = r.get("status")
                if status == "future":
                    item["why"] = "future"
                    continue
                if status == "wait":
                    item["why"] = "unknown"
                    continue
                wait.pop(uid, None)
                moved = True
                mine = results.setdefault(maker, {})
                if status in ("refused", "held", "conflict") or r.get("conflicts"):
                    mine[r.get("uid") or uid] = {
                        k: r.get(k) for k in ("uid", "status", "why", "conflicts", "layer", "fid",
                                              "mine", "theirs", "other")
                    }
                    res_changed = True
                elif uid in mine:
                    del mine[uid]
                    res_changed = True
    if res_changed:
        st.put("pigeon_results", results)
    st.put("peer_wait", _cap_wait(wait))


def retry(node):
    """Retry held ``peer_wait`` ops (future / unknown / editing; guard waits for the stream)."""
    st = node.store
    wait = dict(st.get("peer_wait") or {})
    if not wait:
        sync_status(node)
        return 0
    if node.role == "host" and node.hub is not None:
        _retry_host(node, wait)
        sync_status(node)
        return 0

    refused_list = list(st.get("peer_refused") or [])
    refused_set = set(refused_list)
    refused_dirty = False
    total_applied = 0
    while True:
        batch = []
        drop = []
        for uid, item in list(wait.items()):
            if not isinstance(item, dict):
                drop.append(uid)
                continue
            if item.get("why") == "guard":
                continue
            if st.has(uid) or uid in refused_set:
                drop.append(uid)
                continue
            op = item.get("op")
            maker = item.get("maker")
            who = _roster_entry(node, maker)
            if not op or who is None:
                drop.append(uid)
                continue
            hlc, _ = lww.delta(op)
            if _is_future(node, hlc):
                item["why"] = "future"
                continue
            batch.append((dict(op), maker, who, True))
        for u in drop:
            wait.pop(u, None)
        if not batch:
            break
        applied, wait_updates, refused_adds, done_uids = _apply_batch(node, batch)
        total_applied += applied
        for u in done_uids:
            wait.pop(u, None)
        for u, w in wait_updates.items():
            wait[u] = w
        for u in refused_adds:
            if u not in refused_set:
                refused_list.append(u)
                refused_set.add(u)
                refused_dirty = True
        if not applied:
            break

    st.put("peer_wait", _cap_wait(wait))
    if refused_dirty:
        st.put("peer_refused", _cap_refused(refused_list))
    sync_status(node, applied=total_applied)
    return total_applied
