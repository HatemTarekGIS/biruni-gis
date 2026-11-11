"""Team vault: snapshots of a project's files kept on the computers of the team room, k of n (default 3 of 5).

Content-addressed chunks (store.py) keep each piece of data once across all snapshots; each snapshot's new chunks
are encrypted, cut into n shards with Reed-Solomon (rs.py) and their key into n Shamir shares (shamir.py). No
computer can read what it keeps; any k shards give the snapshot back; lost shards are rebuilt from k (repair).
See SPEC.md. Not to be confused with ``vault`` (the data catalog).

Public API: snapshot(), restore(), health(), repair(), open_dialog().
"""
from . import core, net
from .core import DEFAULT_K, DEFAULT_N, VaultError


def _keeper():
    return net.current()


def _vault():
    k = _keeper()
    return k.vault if k is not None else core.Vault()


def snapshot(paths, label="", k=DEFAULT_K, n=DEFAULT_N, password=None, progress=None, place=True):
    """Take a snapshot of files and folders; in a team room its shards go to the members right away.
    -> core.Vault.snapshot's result, plus "placement" when placed."""
    kp = _keeper()
    v = kp.vault if kp is not None else core.Vault()
    res = v.snapshot(paths, label, k, n, password, progress)
    if place and kp is not None:
        try:
            res["placement"] = kp.place()
        except Exception as e:  # noqa: BLE001 - kept in own/out: placed later
            res["placement"] = {"queued": 0, "waiting": n, "why": str(e)}
    return res


def restore(dest, snap_id=None, password=None, folders=None, timeout=90):
    """Write a snapshot into ``dest``: from shard folders (USB sticks) when given, else from the team room (any k
    shards), else from this computer's own store. ``snap_id``: None = the newest. -> {"files", "bytes", "from"}"""
    if folders:
        found, _bad = core.scan_folders(folders)
        return _restore_found(found, dest, snap_id, password, "folders")
    kp = _keeper()
    if kp is not None:
        kp.refresh()
        packs = set(kp.my_packs_in_room()) | set(kp.vault.outbox())
        if snap_id:
            try:
                packs = set(kp.vault.packs_of(snap_id))
            except VaultError:
                pass
        found = kp.gather(sorted(packs), timeout=timeout)
        out = _restore_found(found, dest, snap_id, password, "team")
        try:
            kp.vault.adopt(found, password)    # a new PC (recovery kit): its snapshots show and can be repaired
        except Exception:  # noqa: BLE001 - the files are written: that is what counts
            pass
        return out
    v = core.Vault()
    if not snap_id:
        s = v.snapshots()
        if not s:
            raise VaultError("no snapshot")
        snap_id = s[0]["id"]
    out = v.restore_local(snap_id, dest)
    out["from"] = "this computer"
    return out


def _restore_found(found, dest, snap_id, password, source):
    snaps = core.snapshots_in(found, password)
    if not snaps:
        raise VaultError("not enough shards of any snapshot are at hand")
    pick = next((s for s in snaps if s["id"] == snap_id), None) if snap_id else snaps[0]
    if pick is None:
        raise VaultError("not enough shards of this snapshot are at hand")
    out = core.restore_from(found, pick["pack"], dest, password)
    out.update({"from": source, "snap": pick["id"], "label": pick["label"]})
    return out


def health():
    kp = _keeper()
    if kp is None:
        v = core.Vault()
        return [dict(s, state="not shared", alive=0, can_lose=None, packs=[]) for s in v.snapshots()]
    return kp.health()


def repair(timeout=90):
    kp = _keeper()
    if kp is None:
        raise VaultError("repair needs the team room")
    return kp.repair(timeout)


def on_room(node):
    net.on_room(node)


def open_dialog(plugin=None):
    from .ui import open_dialog as _open

    return _open(plugin)


def remove(snap_id):
    """Remove a snapshot; its packs no other snapshot needs are withdrawn from the team. -> withdrawn packs"""
    kp = _keeper()
    if kp is not None:
        return kp.withdraw(snap_id)
    v = core.Vault()
    try:
        return v.remove_snapshot(snap_id)
    finally:
        v.close()
