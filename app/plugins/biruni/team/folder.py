"""The room through a shared folder: no one has to be online at the same time (idea I05, the folder carrier).

Any folder the office already shares - a OneDrive / Google Drive / Dropbox client folder, a NAS or SMB share, a
USB stick passed around - carries the room's changes between its computers. Nothing new is invented for the
changes themselves: a file in the folder holds carrier-pigeon packs (pigeon/core.py), sealed with the room key
and signed by their maker, and they are decided exactly as packs carried by hand are:

* a member writes "up" packs (its changes not yet in the room's stream); the host (or whoever took over as host)
  decides them with Hub.apply_remote - signatures, rights, work areas, the delete guard and the three-way merge all
  apply as over the network;
* the host writes "down" packs (runs of the stream, the roster, its answers); members apply them in order.

So the host and the members never need to be online together: the surveyor's edits made in the morning reach the
office PC whenever it next opens, and the office's changes reach the surveyor whenever the folder syncs.

The layout is safe for file-sync services (nobody ever writes a file someone else writes):

    <folder>/biruni-room-<room tag>/
        d-<member id>/                 one folder per computer, written by that computer only
            000012-1f2e3d4c5b6a.bpf    one envelope of packs; written once (temp file, then rename), never changed
            head.bph                   sealed + signed: the files of this folder and their SHA-256

A reader trusts only files listed in a valid head and whose hash matches: a half-synced file, a OneDrive
placeholder not downloaded yet, a ".tmp", a sync service's conflict copy ("head (1).bph", "x-conflict.bpf") or a
file someone dropped in is skipped (until it is complete, or for ever). A head that is not sealed with the room key
or not signed by the member the folder belongs to rejects that whole folder.

Compaction: the host keeps a snapshot file of the whole stream (a down pack from the start), so a computer that
was away for months reads one file instead of years of them, and the host then deletes its own older files that
every member has. Members delete their own up files once all their changes are in the stream.

What the storage provider (or anyone with the folder) can still do - said plainly: see the sizes and times of the
files and which folder is busy; delete or hold back files (changes then wait, nothing wrong is accepted); keep old
copies. It cannot read the changes (AES-256-GCM with the room key) nor forge or alter them (Ed25519 signatures
checked against the room's roster). A member removed from the room is ignored from then on (its folder is not
read, and the host refuses its changes).
"""
import hashlib
import json
import os
import re
import time

from ..secure import crypto
from ..pigeon import core as pigeon

HEAD = "head.bph"
EXT = ".bpf"
AAD = b"biruni-room-folder-1"
SNAP = "snapshot" + EXT
INTERVAL = 15                     # seconds between two looks at the folder
UP_EVERY = 600                    # a member writes a fresh up file at least this often while it has a new place
SNAP_EVERY = 500                  # the host writes a new snapshot after this many new changes in the stream
KEEP_DONE = 5000                  # files remembered as read
_NAME = re.compile(r"^\d{6}-[0-9a-f]{12}\.bpf$")


def room_dir(folder, node):
    return os.path.join(folder, "biruni-room-" + node.keys.tag[:16])


def _write_once(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


class FolderCarrier:
    """Carries one room's packs through one folder. sync() reads what the others wrote and writes this computer's
    own; start() does it every INTERVAL seconds (a QTimer, in the main thread like everything that touches a
    layer)."""

    def __init__(self, node, folder):
        self.node = node
        self.folder = folder
        self.timer = None
        self.keep_days = pigeon.EXPIRY / 86400
        self.status = {"last": None, "devices": 0, "pending": 0, "read": 0, "written": 0, "refused": 0,
                       "error": ""}

    # ---- paths and the head ---------------------------------------------------------------------------------
    @property
    def base(self):
        return room_dir(self.folder, self.node)

    def my_dir(self):
        return os.path.join(self.base, "d-" + self.node.me_id)

    def _head_body(self, files):
        return {"v": 1, "kind": "head", "room": self.node.keys.tag, "maker": self.node.me_id, "made": time.time(),
                "files": files}

    def _read_head(self, d):
        """-> (maker id, {name: sha}) or raises pigeon.PigeonError."""
        try:
            with open(os.path.join(d, HEAD), "rb") as f:
                blob = f.read()
            body = json.loads(crypto.unseal(self.node.keys.key, blob, AAD, max_size=8 << 20).decode("utf-8"))
        except (OSError, crypto.CryptoError, ValueError, UnicodeDecodeError):
            raise pigeon.PigeonError("sealed with another key or altered") from None
        if not isinstance(body, dict) or body.get("kind") != "head" or body.get("room") != self.node.keys.tag:
            raise pigeon.PigeonError("not a pack")
        if not pigeon._signed_ok(body):
            raise pigeon.PigeonError("bad signature")
        maker = str(body.get("maker", ""))
        if os.path.basename(d) != "d-" + maker:
            raise pigeon.PigeonError("signed by someone else")
        who = pigeon._roster_entry(self.node, maker)
        if who is None or not who.get("pub") or who["pub"] != body.get("pub"):
            raise pigeon.PigeonError("not a member")
        if who.get("state") == "out":
            raise pigeon.PigeonError("not a member")
        files = body.get("files") or {}
        return maker, {str(k): str(v) for k, v in files.items() if isinstance(k, str)}

    def _my_files(self):
        """{name: sha} of this computer's folder as its own head says (its own record is the truth)."""
        try:
            return self._read_head(self.my_dir())[1]
        except pigeon.PigeonError:
            return {}

    def _write_head(self, files):
        body = pigeon._sign(self.node, self._head_body(files))
        _write_once(os.path.join(self.my_dir(), HEAD),
                    crypto.seal(self.node.keys.key, crypto.canonical(body), AAD, compress=True))

    # ---- writing ----------------------------------------------------------------------------------------------
    def _own_packs(self, snapshot=False):
        node = self.node
        st = pigeon._ready(node.store)
        if pigeon._role(node) == "host":
            if snapshot:
                saved = st.get("pigeon_seen_map")
                st.put("pigeon_seen_map", {})          # a down run from the very start
                try:
                    return pigeon._down_packs(node)
                finally:
                    st.put("pigeon_seen_map", saved or {})
            return pigeon._down_packs(node)
        node.store.put("pigeon_only", True)   # the host is far, not gone: never take the room over for it
        return pigeon._up_packs(node)

    def _envelope(self, packs):
        blobs = [pigeon.seal(self.node, b) for b in packs]
        return pigeon.MAGIC + crypto.canonical({"room": self.node.keys.tag, "packs": [crypto.b64(b) for b in blobs]})

    def _wants_write(self, host):
        st = self.node.store
        if host:
            head = st.head()
            return head > int(st.get("folder_written_head") or -1)
        uids = sorted(o.get("uid") for o in st.outbox(1 << 30))
        key = _sha(json.dumps([uids, int(self.node.since or 0)]).encode())
        last = st.get("folder_written_key")
        if key != last and (uids or time.time() - float(st.get("folder_written_at") or 0) > UP_EVERY):
            return key
        return None

    def write(self):
        """Write this computer's packs when it has something new. -> number of files written"""
        node = self.node
        host = pigeon._role(node) == "host"
        want = self._wants_write(host)
        if not want:
            return 0
        os.makedirs(self.my_dir(), exist_ok=True)
        files = self._my_files()
        n = 0
        seq = 1 + max([int(k[:6]) for k in files if _NAME.match(k)] + [0])
        data = self._envelope(self._own_packs())
        name = f"{seq:06d}-{_sha(data)[:12]}{EXT}"
        _write_once(os.path.join(self.my_dir(), name), data)
        files[name] = _sha(data)
        n += 1
        st = node.store
        if host:
            head = st.head()
            if head - int(st.get("folder_snap_head") or 0) >= SNAP_EVERY or SNAP not in files:
                snap = self._envelope(self._own_packs(snapshot=True))
                _write_once(os.path.join(self.my_dir(), SNAP), snap)
                files[SNAP] = _sha(snap)
                st.put("folder_snap_head", head)
                n += 1
            st.put("folder_written_head", head)
        else:
            st.put("folder_written_key", want)
            st.put("folder_written_at", time.time())
        self._write_head(files)
        self._tidy(files, host)
        self.status["written"] += n
        return n

    def _tidy(self, files, host):
        """Delete this computer's own files nobody needs any more (only ever its own folder)."""
        st = self.node.store
        keep = dict(files)
        mine = sorted(k for k in files if _NAME.match(k))
        if host:
            # every file but the newest few goes once all members have read past it, or once it is older than
            # keep_days (its packs expire after 30 days anyway): the snapshot, made fresh first, covers them
            seen = st.get("pigeon_seen_map") or {}
            roster = self.node.roster or st.get("roster") or []
            members = [m["id"] for m in roster if not m.get("host") and m.get("state") != "out"
                       and m["id"] != self.node.me_id]
            everyone = bool(members) and all(m in seen for m in members)
            old = time.time() - self.keep_days * 86400
            for k in mine[:-3]:
                try:
                    aged = os.path.getmtime(os.path.join(self.my_dir(), k)) < old
                except OSError:
                    aged = True
                if everyone or aged:
                    keep.pop(k, None)
            if keep != files:
                snap = self._envelope(self._own_packs(snapshot=True))
                _write_once(os.path.join(self.my_dir(), SNAP), snap)
                keep[SNAP] = _sha(snap)
                st.put("folder_snap_head", st.head())
        else:
            # up files whose changes are all in the stream: delivered
            for k in mine[:-1]:
                keep.pop(k, None)
        if keep != files:
            self._write_head(keep)          # the head first: a reader never looks for a file the head still lists
            for k in set(files) - set(keep):
                try:
                    os.remove(os.path.join(self.my_dir(), k))
                except OSError:
                    pass

    # ---- reading ----------------------------------------------------------------------------------------------
    def read(self):
        """Take what the others wrote. -> report (pigeon.open_data's, summed) plus "skipped" and "devices"."""
        node = self.node
        st = node.store
        done = st.get("folder_done") or []
        seen_files = set(done)
        rep = {"packs": 0, "in": 0, "applied": 0, "carried": 0, "waiting": 0, "dups": 0, "refused": [],
               "conflicts": 0, "skipped": 0, "devices": 0, "pending": 0}
        if not os.path.isdir(self.base):
            return rep
        host = pigeon._role(node) == "host"
        for entry in sorted(os.listdir(self.base)):
            d = os.path.join(self.base, entry)
            if not entry.startswith("d-") or not os.path.isdir(d) or entry == "d-" + node.me_id:
                continue
            try:
                maker, files = self._read_head(d)
            except pigeon.PigeonError as e:
                rep["refused"].append("folder " + entry[:14] + ": " + str(e))
                continue
            rep["devices"] += 1
            who = pigeon._roster_entry(node, maker) or {}
            if not host and not who.get("host"):
                continue        # a member reads the host's stream only; the others' up packs are for the host
            names = sorted(files)
            if SNAP in names:
                names.remove(SNAP)
                names.insert(0, SNAP)       # the snapshot first: a computer long away catches up from one file
            for name in names:
                key = entry + "/" + name + ":" + files[name]
                if key in seen_files:
                    continue
                if name != SNAP and not _NAME.match(name):
                    continue
                path = os.path.join(d, name)
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError:
                    rep["pending"] += 1             # listed but not synced yet (or a cloud placeholder)
                    continue
                if _sha(data) != files[name]:
                    rep["pending"] += 1             # half written / half synced / altered: wait, never use
                    continue
                try:
                    r = pigeon.open_data(node, data)
                except pigeon.PigeonError as e:
                    rep["refused"].append(str(e))
                    seen_files.add(key)
                    continue
                for k in ("packs", "in", "applied", "dups", "conflicts"):
                    rep[k] += r.get(k, 0)
                rep["refused"] += r.get("refused", [])
                rep["waiting"] = r.get("waiting", 0)
                if name != SNAP or int(r.get("waiting", 0)) == 0:
                    seen_files.add(key)
            # anything else in the folder (conflict copies, temp files, strangers) is never opened
            rep["skipped"] += sum(1 for x in os.listdir(d) if x != HEAD and x not in files)
        st.put("folder_done", sorted(seen_files)[-KEEP_DONE:])
        return rep

    # ---- the loop ---------------------------------------------------------------------------------------------
    def sync(self):
        try:
            rep = self.read()
            self.write()
            self.status.update(last=time.time(), devices=rep["devices"], pending=rep["pending"],
                               read=self.status["read"] + rep["packs"],
                               refused=self.status["refused"] + len(rep["refused"]), error="")
            return rep
        except OSError as e:
            self.status["error"] = str(e)
            return None

    def start(self, interval=INTERVAL):
        from qgis.PyQt.QtCore import QTimer

        self.stop()
        self.timer = QTimer()
        self.timer.timeout.connect(self.sync)
        self.timer.start(int(interval * 1000))
        self.sync()

    def stop(self):
        if self.timer is not None:
            self.timer.stop()
            self.timer = None


# ---- the room's folder, remembered in its record ----------------------------------------------------------------
_CARRIER = [None]


def current():
    return _CARRIER[0]


def use_folder(node, folder):
    """Carry this room through folder from now on (None: stop). -> the carrier or None"""
    old = _CARRIER[0]
    if old is not None:
        old.stop()
        _CARRIER[0] = None
    node.store.put("folder_carrier", folder or "")
    if not folder:
        return None
    os.makedirs(room_dir(folder, node), exist_ok=True)
    c = FolderCarrier(node, folder)
    _CARRIER[0] = c
    c.start()
    return c


def on_room(node):
    """team.set_current: a room that uses a folder starts carrying again; leaving a room stops it."""
    old = _CARRIER[0]
    if old is not None and (node is None or old.node is not node):
        old.stop()
        _CARRIER[0] = None
    if node is None:
        return
    folder = node.store.get("folder_carrier") or ""
    if folder and os.path.isdir(folder) and _CARRIER[0] is None:
        c = FolderCarrier(node, folder)
        _CARRIER[0] = c
        c.start()
