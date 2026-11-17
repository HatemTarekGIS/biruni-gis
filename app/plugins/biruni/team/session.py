"""Opening a room, joining one, and coming back to one.

* open_room   - the host: the chosen layers become the room's shared layers (copied into a
                GeoPackage when they are another format), a code is made, the server starts.
* join_steps  - a member (in a worker thread): find the host by the code, ask to join, wait for
                the host's approval, download the room's files.
  finish_join - (main thread) build the map in a new folder and start following the room.
* reopen      - opening a map that belongs to a room reconnects by itself.

The room's record lives in <map folder>/.biruni/team/room.sqlite; the code is kept there
encrypted for this Windows user (DPAPI).
"""
import json
import os
import secrets
import time

from qgis.core import (QgsCoordinateReferenceSystem, QgsProject, QgsRectangle, QgsSettings, QgsVectorLayer)

from ..i18n import tr
from ..secure import crypto
from . import cloudfs, code as codes
from . import features, set_current, wire
from .hub import BLOCK, own_block
from .node import Node, download_snapshot
from .snapshot import load_room_map
from .store import Store

PROP = "biruni/team-layer"
IDENT_KEY = "biruni/team/identity"
NAME_KEY = "biruni/team/name"
PENDING_KEY = "biruni/team/pending-joins"
JOIN_POLL = 15


def _protect(text):
    from ..field_photos.tunnel import protect

    return protect(text)


def _unprotect(text):
    from ..field_photos.tunnel import unprotect

    return unprotect(text)


def identity():
    """This Windows user's member identity: a random id and a signing key (kept encrypted)."""
    s = QgsSettings()
    raw = _unprotect(s.value(IDENT_KEY, "") or "")
    try:
        ident = json.loads(raw)
        assert len(ident["id"]) == 32 and len(ident["seed"]) == 64
    except Exception:  # noqa: BLE001 - the first time
        ident = {"id": secrets.token_hex(16), "seed": secrets.token_hex(32)}
        s.setValue(IDENT_KEY, _protect(json.dumps(ident)))
    ident["name"] = my_name()
    return ident


def my_name():
    return QgsSettings().value(NAME_KEY, "") or os.environ.get("USERNAME", "") or "Member"


def set_my_name(name):
    QgsSettings().setValue(NAME_KEY, (name or "").strip()[:60])


def store_path(folder):
    return os.path.join(folder, ".biruni", "team", "room.sqlite")


def shareable(layer):
    """Why a layer cannot be shared, or "" (it can; "convert" when it will be copied into a GeoPackage)."""
    if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
        return "not a vector layer"
    if layer.providerType() == "memory":
        return "convert"
    if layer.providerType() != "ogr":
        return "not a file layer"
    path = layer.source().split("|")[0]
    if not path.lower().endswith(".gpkg"):
        return "convert"
    if not layer.primaryKeyAttributes():
        return "convert"
    return ""


def _gpkg_for(prj):
    from ..project import project_gpkg

    gpkg = project_gpkg(prj)
    if gpkg:
        return gpkg
    stem = os.path.splitext(os.path.basename(prj.fileName()))[0]
    return os.path.join(os.path.dirname(prj.fileName()), stem + "_team.gpkg")


# ---- the host ----------------------------------------------------------------------------------------------
def open_room(layers, name, approve=True, external=False, rights="edit"):
    """Make the current (saved) map a room. Returns the node."""
    prj = QgsProject.instance()
    if not prj.fileName():
        raise ValueError("save")
    from ..project import copy_vector_into_gpkg

    for lyr in layers:
        if lyr.isEditable():
            lyr.commitChanges()
        if shareable(lyr) == "convert":
            copy_vector_into_gpkg(lyr, _gpkg_for(prj))
    folder = os.path.dirname(prj.fileName())
    st = Store(store_path(folder))
    code = codes.new_code()
    keys = codes.RoomKeys(code)
    shared, top = {}, 0
    for i, lyr in enumerate(layers, 1):
        key = f"L{i}"
        lyr.setCustomProperty(PROP, key)
        shared[key] = {"name": lyr.name(), "table": lyr.source().split("layername=")[-1].split("|")[0],
                       "layer_id": lyr.id()}
        top = max(top, features.max_fid(lyr))
    crs = prj.crs()
    st.put("code", _protect(keys.code))
    st.put("room_name", name)
    st.put("layers", shared)
    st.put("crs", crs.authid() or crs.toWkt())
    try:
        from qgis.utils import iface
        e = iface.mapCanvas().extent()
        st.put("extent", [e.xMinimum(), e.yMinimum(), e.xMaximum(), e.yMaximum()])
    except Exception:  # noqa: BLE001 - no window
        pass
    st.put("approve", bool(approve))
    st.put("default_rights", rights)
    st.put("external", bool(external))
    st.put("next_block", (top // BLOCK + 2) * BLOCK)
    st.put("epoch", 1)
    st.put("rank", 0)
    st.put("created", time.time())
    prj.write()
    node = Node(st, keys, identity())
    node.attach_layers({k: lyr for k, lyr in zip(shared, layers)})
    node.start_host(1)
    set_current(node)
    return node


def room_code(node):
    return _unprotect(node.store.get("code", ""))


def close_room(node, forget=False):
    """Stop following the room on this computer (forget: the map is no longer a room's)."""
    node.shutdown()
    set_current(None)
    if forget:
        for lyr in node.layers.values():
            lyr.removeCustomProperty(PROP)
        node.store.put("closed", True)
    node.store.close()


# ---- a member -------------------------------------------------------------------------------------------
def find_host(keys, sealer, outside=True):
    infos = wire.find_on_network(keys, sealer)
    if not infos and outside:
        infos = wire.find_published(keys, sealer)
    infos.sort(key=lambda i: (-int(i.get("on", 0)), -int(i.get("epoch", 0)), int(i.get("rank", 99))))
    return infos


def join_steps(code, parent, report, cancelled=lambda: False, address=None):
    """Worker thread: everything a join needs from the network. report(step, a, b) tells the window.
    Returns what finish_join needs."""
    keys = codes.RoomKeys(code)
    sealer = wire.Sealer(keys.key)
    ident = identity()
    report("finding", 0, 0)
    if address:
        infos = [{"address": address}]
    else:
        infos = []
        for _ in range(4):
            infos = find_host(keys, sealer)
            if infos or cancelled():
                break
    if not infos:
        raise wire.WireError("not-found")
    info = infos[0]
    client = wire.Client(sealer, info["address"])
    pub = crypto.public_key(bytes.fromhex(ident["seed"])).hex()
    while True:
        if cancelled():
            raise wire.WireError("cancelled")
        r = wire.say_hello(client, {"id": ident["id"], "name": ident["name"], "pub": pub,
                                    "xpub": wire.box_key(ident["seed"])[1]}, ident["seed"], keys.tag)
        if r.get("status") == "ok":
            break
        if r.get("status") == "refused":
            raise wire.WireError("refused:" + str(r.get("why", "")))
        report("waiting", 0, 0)
        time.sleep(2)
    room = r["room"]
    from ..project import safe_name

    base = safe_name(room.get("name") or "Room") or "Room"
    folder, n = os.path.join(parent, base), 2
    while os.path.exists(folder) and os.listdir(folder):
        folder, n = os.path.join(parent, f"{base} ({n})"), n + 1
    os.makedirs(folder, exist_ok=True)
    snap = download_snapshot(client, r["member"]["token"], folder, lambda d, t: report("downloading", d, t),
                             cancelled)
    return {"code": keys.code, "address": info["address"], "hello": r, "snap": snap, "folder": folder,
            "name": base}


def save_pending_join(code, parent_folder, drive_folder=None, room_name="", offline_ok=False):
    """Remember a folder join waiting for host approval across restarts (code protected via DPAPI)."""
    keys = codes.RoomKeys(code)
    s = QgsSettings()
    try:
        items = json.loads(s.value(PENDING_KEY, "[]") or "[]")
        if not isinstance(items, list):
            items = []
    except ValueError:
        items = []
    items = [it for it in items if isinstance(it, dict) and it.get("tag") != keys.tag]
    items.append({
        "tag": keys.tag,
        "code": _protect(keys.code),
        "folder": parent_folder,
        "drive_folder": drive_folder or "",
        "room": room_name or "",
        "offline_ok": bool(offline_ok),
        "ts": time.time(),
    })
    s.setValue(PENDING_KEY, json.dumps(items))


def pending_joins():
    """Return pending folder joins with decrypted room codes."""
    s = QgsSettings()
    try:
        items = json.loads(s.value(PENDING_KEY, "[]") or "[]")
        if not isinstance(items, list):
            return []
    except ValueError:
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        code = _unprotect(it.get("code", "") or "")
        if not codes.normalize(code):
            continue
        out.append(dict(it, code=codes.RoomKeys(code).code))
    return out


def remove_pending_join(tag):
    """Remove a pending folder join by room tag."""
    s = QgsSettings()
    try:
        items = json.loads(s.value(PENDING_KEY, "[]") or "[]")
        if not isinstance(items, list):
            return
    except ValueError:
        return
    kept = [it for it in items if isinstance(it, dict) and it.get("tag") != tag]
    if kept:
        s.setValue(PENDING_KEY, json.dumps(kept))
    else:
        s.remove(PENDING_KEY)


def _find_join_drive_folder(keys, drive_folder):
    from ..field_photos import drive
    from . import folder as rf

    candidates = [drive_folder] if drive_folder else cloudfs.find_room(keys.tag, drive.suggest_roots())
    for cand in candidates:
        if not cand:
            continue
        base = os.path.join(cand, "biruni-room-" + keys.tag[:16])
        if os.path.basename(os.path.normpath(cand)) == "biruni-room-" + keys.tag[:16]:
            base = cand
            cand = os.path.dirname(os.path.normpath(cand))
        marker = rf.read_marker(base, keys.key, keys.tag)
        if marker is not None:
            return cand, base, marker
    return None, None, None


def join_from_folder(code, rooms_parent, drive_folder=None, report=lambda *a: None,
                     cancelled=lambda: False, offline_ok=False, poll_once=False):
    """Worker thread: request to join a room through a synced folder and unpack the welcome + base package."""
    from . import folder as rf

    keys = codes.RoomKeys(code)
    ident = identity()
    box_priv, _xpub = wire.box_key(ident["seed"])
    drive_folder, base, marker = _find_join_drive_folder(keys, drive_folder)
    if drive_folder is None or marker is None:
        raise wire.WireError("no-folder")

    save_pending_join(keys.code, rooms_parent, drive_folder=drive_folder,
                      room_name=marker.get("name", ""), offline_ok=offline_ok)
    _req_path, _req_n = rf.write_join_request(base, keys, ident)
    last_write = time.time()
    report("asked", 0, 0)

    while True:
        if cancelled():
            raise wire.WireError("cancelled")
        marker = rf.read_marker(base, keys.key, keys.tag)
        if marker is not None:
            host_dir = os.path.join(base, "d-" + marker["maker"])
            if os.path.isdir(host_dir):
                try:
                    entries = os.listdir(host_dir)
                except OSError:
                    entries = []
                _hn, _fn, hbody, *_ = rf._pick_head(
                    host_dir, entries, keys.key, keys.tag, roster=None,
                    trusted_pub=marker["pub"], check_placeholder=True,
                )
                if hbody is not None:
                    fmap = {str(k): str(v) for k, v in (hbody.get("files") or {}).items()
                            if isinstance(k, str)}
                    prefix = ident["id"][:16]
                    welcomes = []
                    for fname in fmap:
                        m = rf._WELCOME_RE.match(fname)
                        if m and m.group(1) == prefix:
                            welcomes.append((int(m.group(2)), fname))
                    welcomes.sort(key=lambda x: -x[0])
                    for _wn, wname in welcomes:
                        wpath = os.path.join(host_dir, wname)
                        if cloudfs.placeholder(wpath) is True:
                            continue
                        try:
                            if os.path.getsize(wpath) > rf.MAX_READ:
                                continue
                            wblob = rf._read(wpath)
                        except OSError:
                            continue
                        if len(wblob) > rf.MAX_READ or rf._sha(wblob) != fmap[wname]:
                            continue
                        try:
                            wbody = rf.unseal_welcome(wblob, box_priv, ident["id"],
                                                      marker["pub"], marker["maker"], keys.tag)
                        except Exception:  # noqa: BLE001
                            continue
                        if wbody.get("kind") == "refused":
                            remove_pending_join(keys.tag)
                            raise wire.WireError("refused:")
                        if wbody.get("kind") == "welcome" and wbody.get("base"):
                            base_info = wbody["base"]
                            parts = base_info.get("parts") or []
                            parts_ready = True
                            for p in parts:
                                pname = p["name"]
                                ppath = os.path.join(host_dir, pname)
                                if fmap.get(pname) != p["sha"] or cloudfs.placeholder(ppath) is True:
                                    parts_ready = False
                                    break
                                try:
                                    if os.path.getsize(ppath) < int(p.get("size") or 0):
                                        parts_ready = False
                                        break
                                except OSError:
                                    parts_ready = False
                                    break
                            if parts_ready:
                                from ..project import safe_name

                                room = wbody["room_info"]
                                base_name = safe_name(room.get("name") or "Room") or "Room"
                                folder, n = os.path.join(rooms_parent, base_name), 2
                                while os.path.exists(folder) and os.listdir(folder):
                                    folder, n = os.path.join(rooms_parent, f"{base_name} ({n})"), n + 1
                                os.makedirs(folder, exist_ok=True)
                                sub = rf._unique_data_dir(folder)
                                try:
                                    snap = rf.extract_base(host_dir, base_info, sub, report=report)
                                except ValueError as e:
                                    raise wire.WireError(str(e)) from None
                                remove_pending_join(keys.tag)
                                return {
                                    "code": keys.code,
                                    "address": None,
                                    "hello": {"member": wbody["member"], "room": room},
                                    "snap": snap,
                                    "folder": folder,
                                    "name": base_name,
                                    "left_out": base_info.get("left_out") or [],
                                    "drive_folder": drive_folder,
                                    "offline_ok": bool(offline_ok),
                                }
        if poll_once:
            return None
        report("waiting", 0, 0)
        if time.time() - last_write >= rf.REFRESH:
            rf.write_join_request(base, keys, ident)
            last_write = time.time()
        end = time.monotonic() + JOIN_POLL
        while time.monotonic() < end:
            if cancelled():
                raise wire.WireError("cancelled")
            time.sleep(min(0.25, max(0.01, end - time.monotonic())))


def finish_join(res):
    """Main thread: the room's map in its folder, then follow the room."""
    prj = QgsProject.instance()
    prj.clear()
    room, snap, folder = res["hello"]["room"], res["snap"], res["folder"]
    crs = QgsCoordinateReferenceSystem(room.get("crs") or "")
    if crs.isValid():
        prj.setCrs(crs)
    shared = room.get("layers", {})
    layer_files = {v["layer_id"]: snap["layer_files"][k] for k, v in shared.items() if k in snap["layer_files"]}
    tables = {v["layer_id"]: v["table"] for v in shared.values()}
    gone = load_room_map(snap["folder"], layer_files, tables, snap.get("extra", {}))
    layers = {}
    for key, v in shared.items():
        want = os.path.normcase(os.path.normpath(os.path.join(snap["folder"], snap["layer_files"].get(key, ""))))
        for lyr in prj.mapLayers().values():
            if isinstance(lyr, QgsVectorLayer):
                src = lyr.source().split("|")
                if (os.path.normcase(os.path.normpath(src[0])) == want
                        and any(p == "layername=" + v["table"] for p in src[1:])):
                    lyr.setCustomProperty(PROP, key)
                    layers[key] = lyr
    path = os.path.join(folder, res["name"] + ".qgz")
    prj.setFileName(path)
    prj.write()
    st = Store(store_path(folder))
    st.put("code", _protect(res["code"]))
    st.put("room_name", room.get("name", ""))
    st.put("layers", shared)
    st.put("crs", room.get("crs", ""))
    st.put("epoch", int(room.get("epoch", 1)))
    st.put("sid", room.get("sid"))
    for k, v in (room.get("settings") or {}).items():
        st.put(k, v)
    st.put("since", snap["head"])
    st.put("block", own_block(identity()["id"], 0))   # this computer's own ids (hub.own_block)
    st.put("block_n", 1)
    st.put("roster", room.get("members", []))
    st.set_versions([tuple(v) for v in snap["versions"]])
    left_out = res.get("left_out") or []
    if left_out:
        st.put("folder_left_out", left_out)
    keys = codes.RoomKeys(res["code"])
    remove_pending_join(keys.tag)
    node = Node(st, keys, identity())
    node.attach_layers(layers)
    if res.get("address") is None:
        from . import folder as rf

        node.role = "member"
        node.member = res["hello"]["member"]
        st.put("role", "member")
        st.put("rank", node.member.get("rank", 99))
        st.put("pigeon_only", True)
        if res.get("offline_ok"):
            st.put("folder_offline_ok", True)
        node.state = "reconnecting"
        node._apply_rights(node.member.get("rights") or {})
        node._tick.start()
        set_current(node)
        rf.use_folder(node, res["drive_folder"])
    else:
        node.start_member(res["address"], res["hello"]["member"])
        node.was_connected = True
        set_current(node)
    try:
        from qgis.utils import iface
        ext = room.get("extent")
        if ext:
            iface.mapCanvas().setExtent(QgsRectangle(*ext))
            iface.mapCanvas().refresh()
    except Exception:  # noqa: BLE001
        pass
    why_map = {"raster": tr("raster"), "too large": tr("too large")}
    by_name = {x["name"]: why_map.get(x["why"], x["why"]) for x in left_out}
    skipped = []
    seen_names = set()
    for g in gone or []:
        seen_names.add(g)
        skipped.append(f"{g} ({by_name[g]})" if g in by_name else g)
    for x in left_out:
        if x["name"] not in seen_names:
            skipped.append(f"{x['name']} ({by_name[x['name']]})")
    return node, skipped


# ---- coming back -----------------------------------------------------------------------------------------
def room_layers(prj=None):
    prj = prj or QgsProject.instance()
    out = {}
    for lyr in prj.mapLayers().values():
        key = lyr.customProperty(PROP, "")
        if key and isinstance(lyr, QgsVectorLayer):
            out[key] = lyr
    return out


def reopen(prj=None):
    """The current map belongs to a room: follow it again. Returns the node, or None."""
    prj = prj or QgsProject.instance()
    layers = room_layers(prj)
    if not layers or not prj.fileName():
        return None
    path = store_path(os.path.dirname(prj.fileName()))
    if not os.path.isfile(path):
        return None
    st = Store(path)
    if st.get("closed"):
        st.close()
        return None
    code = _unprotect(st.get("code", ""))
    if not codes.normalize(code):
        st.close()
        return None
    node = Node(st, codes.RoomKeys(code), identity())
    node.attach_layers(layers)
    set_current(node)
    if st.get("role") == "host":
        others = [i for i in wire.find_on_network(node.keys, node.sealer, wait=1.0) if i.get("hid") != node.me_id]
        if others:
            node.rejoin(sorted(others, key=lambda i: (-int(i.get("on", 0)), -int(i.get("epoch", 0))))[0])
        else:
            node.start_host()
    else:
        node.role = "member"
        node.member = next((m for m in node.roster if m.get("id") == node.me_id), None)
        node.state = "reconnecting"
        node._lost_since = time.time()
        node._tick.start()
        node._pres_timer.start()
        node.changed.emit()
    return node
