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
        room-0001.bpr                  write-once marker sealed + signed by the host
        d-<member id>/                 one folder per computer, written by that computer only
            000012-1f2e3d4c5b6a.bpf    write-once envelope of packs; temp file then rename, never changed
            snap-00000500-00.bpf       write-once snapshot parts (last DOWN_KEEP entries)
            head-000001.bph            write-once, sealed + signed: files, SHA-256s, and host "from"

A reader trusts only files listed in a valid head and whose hash matches: a half-synced file, a cloud placeholder
not downloaded yet, a ".tmp", a sync service's conflict copy ("head-000001 (1).bph", "x (1).bpf") or a file
someone dropped in is skipped (until it is complete, or for ever). A head that is not sealed with the room key
or not signed by the member the folder belongs to rejects that whole folder.

Compaction: the host keeps a write-once snapshot of the last DOWN_KEEP entries so a member away for weeks reads
one snapshot instead of many incrementals, and drops incrementals everyone has or older than keep_days.

Security:
- Folder access without the code: sees ids, sizes and times. It can delete, hold back or replay files, which is
  always detected or harmless. It cannot read or forge.
- The marker stops a silent re-creation, not a deletion.
"""
import hashlib
import json
import os
import queue
import re
import threading
import time

from ..pigeon import core as pigeon
from ..secure import crypto
from . import cloudfs

HEAD = "head.bph"
EXT = ".bpf"
AAD = b"biruni-room-folder-1"
MARKER_AAD = b"biruni-room-marker-1"
SNAP = "snapshot" + EXT
INTERVAL = 15                     # seconds between two scans
UP_EVERY = 600                    # member refreshes idle up head at most this often when key changed
SNAP_EVERY = 500                  # host writes a fresh snapshot after this many new stream entries
KEEP_DONE = 5000                  # files remembered as read
WRITE_EVERY = 60                  # minimum seconds between writes for both roles (unless force=True)
REFRESH = pigeon.EXPIRY / 3       # rewrite snapshot / outbox before packs expire
HEARTBEAT = 86400                 # host rewrites its head at least once a day
SILENT_DAYS = 7                   # member reports host-silent when newest host head is older than this
MAX_READ = 64 << 20               # maximum bytes read from a single folder file
MAX_INCR = 200                    # maximum incremental files kept in the host's folder

_NAME = re.compile(r"^\d{6}-[0-9a-f]{12}\.bpf$")
_HEAD_RE = re.compile(r"^head-(\d{6})\.bph$")
_SNAP_RE = re.compile(r"^snap-(\d{8})-(\d{2})\.bpf$")
_MARKER_RE = re.compile(r"^room-(\d{4})\.bpr$")


class FolderError(Exception):
    """Folder setup failed: args[0] is 'not-host', 'no-room-here', or 'no-parent'."""


def room_dir(folder, node):
    return os.path.join(folder, "biruni-room-" + node.keys.tag[:16])


def _read(path):
    """Read a file's bytes (module-level seam for tests)."""
    with open(path, "rb") as f:
        return f.read()


def _write_once(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _node_roster(node):
    if node.hub is not None:
        return [dict(m) for m in node.hub.roster()]
    return [dict(m) for m in (node.roster or node.store.get("roster") or [])]


def _marker_name(epoch):
    return f"room-{int(epoch):04d}.bpr"


def read_marker(base, key, tag, roster=None):
    """Return the newest valid room marker dict in ``base``, or None."""
    if not base or not os.path.isdir(base):
        return None
    try:
        entries = os.listdir(base)
    except OSError:
        return None
    cands = []
    for fname in entries:
        m = _MARKER_RE.match(fname)
        if m:
            cands.append((int(m.group(1)), fname))
    cands.sort(key=lambda x: -x[0])
    for file_epoch, fname in cands:
        path = os.path.join(base, fname)
        try:
            if os.path.getsize(path) > MAX_READ:
                continue
            blob = _read(path)
        except OSError:
            continue
        if len(blob) > MAX_READ:
            continue
        try:
            body = json.loads(crypto.unseal(key, blob, MARKER_AAD, max_size=8 << 20).decode("utf-8"))
        except (crypto.CryptoError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(body, dict) or body.get("kind") != "marker" or body.get("room") != tag:
            continue
        if not pigeon._signed_ok(body):
            continue
        ep = body.get("epoch")
        if type(ep) is not int or ep != file_epoch:
            continue
        if roster is not None:
            maker = str(body.get("maker") or "")
            who = next((m for m in roster if m.get("id") == maker), None)
            if who is None or who.get("state") == "out" or not who.get("pub") or who["pub"] != body.get("pub"):
                continue
        return body
    return None


def create_room_folder(node, parent):
    """Host only: create the room directory under ``parent`` and write the room marker."""
    if pigeon._role(node) != "host":
        raise FolderError("not-host")
    if not parent or not os.path.isdir(parent):
        raise FolderError("no-parent")
    base = room_dir(parent, node)
    if not os.path.isdir(base):
        os.mkdir(base)
    epoch = int(node.epoch or 1)
    body = {
        "v": 1,
        "kind": "marker",
        "room": node.keys.tag,
        "maker": node.me_id,
        "name": node.store.get("room_name", ""),
        "made": time.time(),
        "sid": node.store.get("sid"),
        "epoch": epoch,
        "at": 0,
        "prev_sid": None,
    }
    blob = crypto.seal(node.keys.key, crypto.canonical(pigeon._sign(node, body)), MARKER_AAD)
    _write_once(os.path.join(base, _marker_name(epoch)), blob)
    node.store.put("folder_made", node.keys.tag)
    return base


def _head_candidates(entries):
    """Return [(file_n, filename)] sorted highest n first for write-once and legacy heads."""
    cands = []
    for fname in entries:
        m = _HEAD_RE.match(fname)
        if m:
            cands.append((int(m.group(1)), fname))
        elif fname == HEAD:
            cands.append((0, fname))
    cands.sort(key=lambda x: -x[0])
    return cands


def _pick_head(d, entries, key, tag, roster, head_seen=None, check_placeholder=True):
    """Find and verify the newest valid head in ``d``.

    Returns (fname, file_n, body, placeholders, unreadable, last_err, stale_ignored).
    """
    cands = _head_candidates(entries)
    placeholders = 0
    unreadable = 0
    last_err = ""
    stale_ignored = False
    entry = os.path.basename(d)
    for file_n, fname in cands:
        path = os.path.join(d, fname)
        if check_placeholder and cloudfs.placeholder(path) is True:
            placeholders += 1
            continue
        try:
            if os.path.getsize(path) > MAX_READ:
                last_err = "too large"
                continue
            blob = _read(path)
        except OSError:
            unreadable += 1
            last_err = "unreadable"
            continue
        if len(blob) > MAX_READ:
            last_err = "too large"
            continue
        try:
            body = json.loads(crypto.unseal(key, blob, AAD, max_size=8 << 20).decode("utf-8"))
        except (crypto.CryptoError, ValueError, UnicodeDecodeError):
            last_err = "sealed with another key or altered"
            continue
        if not isinstance(body, dict) or body.get("kind") != "head" or body.get("room") != tag:
            last_err = "not a pack"
            continue
        if not pigeon._signed_ok(body):
            last_err = "bad signature"
            continue
        maker = str(body.get("maker", ""))
        if entry != "d-" + maker:
            last_err = "signed by someone else"
            continue
        who = next((m for m in roster if m.get("id") == maker), None)
        if who is None or not who.get("pub") or who["pub"] != body.get("pub") or who.get("state") == "out":
            last_err = "not a member"
            continue
        body_n = body.get("n", 0) if file_n == 0 else body.get("n")
        if type(body_n) is not int or body_n != file_n:
            last_err = "bad head number"
            continue
        if head_seen is not None and maker in head_seen and file_n < int(head_seen[maker]):
            stale_ignored = True
            continue
        return fname, file_n, body, placeholders, unreadable, last_err, stale_ignored
    if not cands:
        last_err = "no head"
    return None, 0, None, placeholders, unreadable, last_err, stale_ignored


def _is_snap(name):
    return name == SNAP or bool(_SNAP_RE.match(name))


def _sort_files(names):
    """Order snap files first (legacy then numbered by hi, part), then incrementals."""
    snaps = []
    incrs = []
    for name in names:
        if name == SNAP:
            snaps.append(((0, 0, 0), name))
        else:
            m = _SNAP_RE.match(name)
            if m:
                snaps.append(((1, int(m.group(1)), int(m.group(2))), name))
            elif _NAME.match(name):
                incrs.append(name)
    snaps.sort(key=lambda x: x[0])
    incrs.sort()
    return [n for _, n in snaps] + incrs


def _from_seq(files, file_after, file_hi, head):
    """Lowest ``after`` contiguously reaching ``head`` across the files in ``files``."""
    if not files:
        return max(0, head - pigeon.DOWN_KEEP)
    intervals = sorted(
        ((int(file_after.get(k, 0)), int(file_hi.get(k, head))) for k in files),
        key=lambda x: (-x[1], x[0]),
    )
    cur = head
    for after, hi in intervals:
        if hi >= cur:
            cur = min(cur, after)
    return cur


def _perform_writes(my_dir, writes, deletes):
    """Execute writes (data files first, heads last) and then deletes."""
    if not writes and not deletes:
        return
    if not os.path.isdir(my_dir):
        os.mkdir(my_dir)
    for path, data, is_head in writes:
        if not is_head:
            _write_once(path, data)
    for path, data, is_head in writes:
        if is_head:
            _write_once(path, data)
    for path in deletes:
        try:
            os.remove(path)
        except OSError:
            pass


def _initial_state(folder, base):
    if not folder or not os.path.isdir(folder):
        return "no-drive"
    if not os.path.isdir(base):
        return "missing"
    return "ok"


class FolderCarrier:
    """Carries one room's packs through one folder.

    sync() runs synchronously for tests and manual triggers; start() polls in a worker thread every INTERVAL
    seconds while draining results on a 1 s QTimer in the main thread.
    """

    def __init__(self, node, folder):
        self.node = node
        self.folder = folder
        self.timer = None
        self.keep_days = pigeon.EXPIRY / 86400
        self._interval = INTERVAL
        self._q = queue.Queue()
        self._stop_ev = threading.Event()
        self._scan_thread = None
        self._last_scan = 0.0
        self._io_lock = threading.Lock()
        self._pending = ([], [], {})
        self._inflight_meta = {}
        self._miss_scans = 0
        self._marker_ok = None   # None: not scanned yet (then checked once); after: the last scan's verdict
        self.status = {
            "last": None,
            "devices": 0,
            "pending": 0,
            "read": 0,
            "written": 0,
            "refused": 0,
            "error": "",
            "state": _initial_state(folder, self.base),
            "host_made": None,
            "heads": {},
            "from": None,
            "streamed": False,
            "placeholders": 0,
            "slow": False,
            "unreadable": 0,
        }

    # ---- paths and heads ------------------------------------------------------------------------------------
    @property
    def base(self):
        return room_dir(self.folder, self.node)

    def my_dir(self):
        return os.path.join(self.base, "d-" + self.node.me_id)

    def _head_body(self, files, n, from_seq=None):
        body = {
            "v": 1,
            "kind": "head",
            "room": self.node.keys.tag,
            "maker": self.node.me_id,
            "made": time.time(),
            "n": n,
            "files": files,
        }
        if from_seq is not None:
            body["from"] = int(from_seq)
        return body

    def _my_files(self):
        """{name: sha} of this computer's folder as its newest valid head says."""
        d = self.my_dir()
        if not os.path.isdir(d):
            return {}
        try:
            entries = os.listdir(d)
        except OSError:
            return {}
        roster = _node_roster(self.node)
        _fname, _fn, body, *_ = _pick_head(
            d, entries, self.node.keys.key, self.node.keys.tag, roster, head_seen=None, check_placeholder=False
        )
        if body is None:
            return {}
        files = body.get("files") or {}
        return {str(k): str(v) for k, v in files.items() if isinstance(k, str)}

    def _old_head_deletes(self, new_head_name):
        """Paths of own heads to delete after writing ``new_head_name`` (keep newest 2)."""
        d = self.my_dir()
        if not os.path.isdir(d):
            return []
        try:
            cands = _head_candidates(os.listdir(d))
        except OSError:
            return []
        existing = [name for _, name in cands if name != new_head_name]
        # Keep the newest 1 existing head alongside the newly written head (2 total).
        return [os.path.join(d, name) for name in existing[1:]]

    # ---- writing --------------------------------------------------------------------------------------------
    def _build_writes(self, force=False):
        """Build (writes, deletes, meta_updates) on the main thread without writing to disk."""
        node = self.node
        st = node.store
        host = pigeon._role(node) == "host"
        roster = _node_roster(node)
        if self._marker_ok is None:
            self._marker_ok = read_marker(self.base, node.keys.key, node.keys.tag, roster or None) is not None
        if not self._marker_ok or not os.path.isdir(self.base):
            return [], [], {}

        now = time.time()
        last_at = st.get("folder_written_at")
        if not force and last_at is not None and now - float(last_at) < WRITE_EVERY:
            return [], [], {}

        my_dir = self.my_dir()
        files = self._my_files()
        writes = []
        deletes = []
        meta = {}
        seq = 1 + max([int(k[:6]) for k in files if _NAME.match(k)] + [0])

        if host:
            from .hub import room_settings

            head = st.head()
            settings_key = _sha(json.dumps(room_settings(st), sort_keys=True).encode())
            written_head = st.get("folder_written_head")
            written_settings = st.get("folder_written_settings")
            need_incr = written_head is None or head > int(written_head) or settings_key != written_settings
            has_snap = any(_is_snap(k) for k in files)
            snap_head = int(st.get("folder_snap_head") or 0)
            snap_at = st.get("folder_snap_at")
            need_snap = (
                not has_snap
                or head - snap_head >= SNAP_EVERY
                or snap_at is None
                or (now - float(snap_at)) >= REFRESH
            )
            head_at = st.get("folder_head_at")
            need_head = head_at is None or (now - float(head_at)) >= HEARTBEAT
            if not (need_incr or need_snap or need_head):
                return [], [], {}

            file_hi = dict(st.get("folder_file_hi") or {})
            file_after = dict(st.get("folder_file_after") or {})
            file_made = dict(st.get("folder_file_made") or {})
            just_written = {}

            if need_incr:
                low = max(int(written_head or 0), head - pigeon.DOWN_KEEP, 0)
                if written_head is not None and low > int(written_head):
                    need_snap = True
                packs = pigeon._down_packs(node, low=low)
                blobs = [pigeon.seal(node, b) for b in packs]
                for data in pigeon.envelopes(blobs, node.keys.tag):
                    name = f"{seq:06d}-{_sha(data)[:12]}{EXT}"
                    seq += 1
                    writes.append((os.path.join(my_dir, name), data, False))
                    files[name] = _sha(data)
                    file_hi[name] = head
                    file_after[name] = low
                    file_made[name] = now
                    just_written[name] = len(writes) - 1
                meta["folder_written_head"] = head
                meta["folder_written_settings"] = settings_key

            # Decide which incrementals to drop via _tidy rules.
            mine = sorted(k for k in files if _NAME.match(k))
            seen = st.get("pigeon_seen_map") or {}
            members = [
                m["id"] for m in roster
                if not m.get("host") and m.get("state") != "out" and m["id"] != node.me_id
            ]
            min_seen = min(int(seen[m]) for m in members) if members and all(m in seen for m in members) else None
            old = now - self.keep_days * 86400
            excess = max(0, len(mine) - MAX_INCR)
            min_keep = max(0, head - pigeon.DOWN_KEEP)
            drop = set()
            for idx, k in enumerate(mine):
                hi = file_hi.get(k)
                by_seen = min_seen is not None and hi is not None and int(hi) <= min_seen
                made = file_made.get(k)
                if made is None:
                    try:
                        made = os.path.getmtime(os.path.join(my_dir, k))
                    except OSError:
                        made = 0
                by_age = (made < old) or (self.keep_days <= 0 and k not in just_written)
                by_count = idx < excess
                unreachable = hi is not None and int(hi) < min_keep
                if by_seen or by_age or by_count or unreachable:
                    drop.add(k)

            if drop:
                need_snap = True

            if need_snap:
                for k in list(files):
                    if _is_snap(k):
                        files.pop(k, None)
                        deletes.append(os.path.join(my_dir, k))
                snap_low = max(0, head - pigeon.DOWN_KEEP)
                spacks = pigeon._down_packs(node, low=snap_low)
                sblobs = [pigeon.seal(node, b) for b in spacks]
                for part, data in enumerate(pigeon.envelopes(sblobs, node.keys.tag)):
                    sname = f"snap-{head:08d}-{part:02d}{EXT}"
                    writes.append((os.path.join(my_dir, sname), data, False))
                    files[sname] = _sha(data)
                    file_hi[sname] = head
                    file_after[sname] = snap_low
                    file_made[sname] = now
                meta["folder_snap_head"] = head
                meta["folder_snap_at"] = now

            if drop:
                drop_indices = set()
                for k in drop:
                    files.pop(k, None)
                    if k in just_written:
                        drop_indices.add(just_written[k])
                    else:
                        deletes.append(os.path.join(my_dir, k))
                if drop_indices:
                    writes = [w for i, w in enumerate(writes) if i not in drop_indices]

            from_seq = _from_seq(files, file_after, file_hi, head)
            n = int(st.get("folder_head_n") or 0) + 1
            head_name = f"head-{n:06d}.bph"
            head_body = pigeon._sign(node, self._head_body(files, n, from_seq=from_seq))
            head_blob = crypto.seal(node.keys.key, crypto.canonical(head_body), AAD, compress=True)
            writes.append((os.path.join(my_dir, head_name), head_blob, True))
            deletes.extend(self._old_head_deletes(head_name))
            written_paths = {w[0] for w in writes}
            deletes = [d for d in deletes if d not in written_paths]

            meta["folder_file_hi"] = {k: v for k, v in file_hi.items() if k in files}
            meta["folder_file_after"] = {k: v for k, v in file_after.items() if k in files}
            meta["folder_file_made"] = {k: v for k, v in file_made.items() if k in files}
            meta["folder_head_n"] = n
            meta["folder_head_at"] = now
            meta["folder_written_at"] = now
            meta["_written_n"] = sum(1 for w in writes if not w[2])
            return writes, deletes, meta

        # Member write
        uids = sorted(o.get("uid") for o in st.outbox(1 << 30))
        key = _sha(json.dumps([uids, int(node.since or 0)]).encode())
        last_key = st.get("folder_written_key")
        age = (now - float(last_at)) if last_at is not None else None
        want_change = key != last_key and (bool(uids) or age is None or age > UP_EVERY)
        want_refresh = bool(uids) and age is not None and age >= REFRESH
        if not (want_change or want_refresh):
            return [], [], {}

        st.put("pigeon_only", True)     # the host is far, not gone: never take the room over for it
        packs = pigeon._up_packs(node)
        blobs = [pigeon.seal(node, b) for b in packs]
        new_names = []
        for data in pigeon.envelopes(blobs, node.keys.tag):
            name = f"{seq:06d}-{_sha(data)[:12]}{EXT}"
            seq += 1
            writes.append((os.path.join(my_dir, name), data, False))
            files[name] = _sha(data)
            new_names.append(name)

        mine = sorted(k for k in files if _NAME.match(k))
        keep_n = max(1, len(new_names))
        for k in mine[:-keep_n]:
            files.pop(k, None)
            deletes.append(os.path.join(my_dir, k))

        n = int(st.get("folder_head_n") or 0) + 1
        head_name = f"head-{n:06d}.bph"
        head_body = pigeon._sign(node, self._head_body(files, n))
        head_blob = crypto.seal(node.keys.key, crypto.canonical(head_body), AAD, compress=True)
        writes.append((os.path.join(my_dir, head_name), head_blob, True))
        deletes.extend(self._old_head_deletes(head_name))

        meta["folder_written_key"] = key
        meta["folder_written_at"] = now
        meta["folder_head_n"] = n
        meta["_written_n"] = len(new_names)
        return writes, deletes, meta

    def _commit_meta(self, meta):
        st = self.node.store
        for k, v in meta.items():
            if not k.startswith("_"):
                st.put(k, v)
        self.status["written"] += int(meta.get("_written_n", 0))

    def write(self, force=False):
        """Write this computer's packs when it has something new. -> number of data files written."""
        writes, deletes, meta = self._build_writes(force=force)
        if not writes:
            return 0
        roster = _node_roster(self.node)
        with self._io_lock:
            if (not os.path.isdir(self.base)
                    or read_marker(self.base, self.node.keys.key, self.node.keys.tag, roster or None) is None):
                return 0
            _perform_writes(self.my_dir(), writes, deletes)
        self._commit_meta(meta)
        return int(meta.get("_written_n", 0))

    # ---- reading (worker thread + main thread) --------------------------------------------------------------
    def _ctx(self):
        """Snapshot of state needed by _scan(), taken on the main thread."""
        st = self.node.store
        return {
            "key": self.node.keys.key,
            "tag": self.node.keys.tag,
            "me": self.node.me_id,
            "host": pigeon._role(self.node) == "host",
            "roster": _node_roster(self.node),
            "done": set(st.get("folder_done") or []),
            "head_seen": dict(st.get("folder_head_seen") or {}),
            "base": self.base,
            "folder": self.folder,
            "since": int(self.node.since or 0),
        }

    def _scan(self, ctx, writes, deletes):
        """Worker thread: write, delete, verify heads, and read files. Never touches node, store or Qt."""
        folder = ctx["folder"]
        base = ctx["base"]
        streamed = (cloudfs.kind(folder) == "gdrive-stream") if folder else False
        out = {
            "state": "ok",
            "folders": [],
            "pending": 0,
            "skipped": 0,
            "refused": [],
            "placeholders": 0,
            "slow": False,
            "unreadable": 0,
            "streamed": streamed,
            "host_made": None,
            "heads": {},
            "from": None,
            "wrote_ok": False,
            "wrote_n": 0,
            "error": "",
        }
        if not folder or not os.path.isdir(folder):
            out["state"] = "no-drive"
            return out
        if not os.path.isdir(base):
            out["state"] = "missing"
            return out
        roster = ctx["roster"]
        if read_marker(base, ctx["key"], ctx["tag"], roster or None) is None:
            out["state"] = "no-marker"
            return out

        my_dir = os.path.join(base, "d-" + ctx["me"])
        if writes or deletes:
            try:
                _perform_writes(my_dir, writes, deletes)
                out["wrote_ok"] = True
                out["wrote_n"] = sum(1 for w in writes if not w[2])
            except OSError as e:
                out["state"] = "error"
                out["error"] = str(e)
                return out

        t0 = time.monotonic()
        try:
            entries = sorted(os.listdir(base))
        except OSError as e:
            out["state"] = "error"
            out["error"] = str(e)
            return out

        for entry in entries:
            d = os.path.join(base, entry)
            if not entry.startswith("d-") or not os.path.isdir(d) or entry == "d-" + ctx["me"]:
                continue
            try:
                d_files = os.listdir(d)
            except OSError:
                out["unreadable"] += 1
                continue
            head_name, file_n, body, h_ph, h_unr, last_err, stale = _pick_head(
                d, d_files, ctx["key"], ctx["tag"], roster, head_seen=ctx["head_seen"], check_placeholder=True
            )
            out["placeholders"] += h_ph
            out["unreadable"] += h_unr
            out["pending"] += h_ph
            if body is None:
                if last_err:
                    out["refused"].append("folder " + entry[:14] + ": " + last_err)
                continue

            maker = str(body.get("maker", ""))
            made = float(body.get("made") or 0)
            out["heads"][maker] = made
            who = next((m for m in roster if m.get("id") == maker), {})
            h_from = body.get("from")
            if who.get("host"):
                out["host_made"] = made
                out["from"] = int(h_from) if h_from is not None else None

            files_map = {str(k): str(v) for k, v in (body.get("files") or {}).items() if isinstance(k, str)}
            f_entry = {
                "entry": entry,
                "maker": maker,
                "n": file_n,
                "made": made,
                "from": int(h_from) if h_from is not None else None,
                "files": [],
            }
            out["folders"].append(f_entry)

            if not ctx["host"] and not who.get("host"):
                continue
            if not ctx["host"] and who.get("host") and h_from is not None and ctx["since"] < int(h_from):
                continue

            for name in _sort_files(files_map):
                sha = files_map[name]
                key = entry + "/" + name + ":" + sha
                if key in ctx["done"]:
                    continue
                fpath = os.path.join(d, name)
                if cloudfs.placeholder(fpath) is True:
                    out["placeholders"] += 1
                    out["pending"] += 1
                    continue
                try:
                    if os.path.getsize(fpath) > MAX_READ:
                        out["refused"].append("too large")
                        continue
                    data = _read(fpath)
                except OSError:
                    out["unreadable"] += 1
                    out["pending"] += 1
                    continue
                if len(data) > MAX_READ:
                    out["refused"].append("too large")
                    continue
                if _sha(data) != sha:
                    out["pending"] += 1
                    continue
                f_entry["files"].append((name, sha, data))

            out["skipped"] += sum(
                1 for x in d_files if x not in files_map and x != HEAD and not _HEAD_RE.match(x)
            )

        out["slow"] = (time.monotonic() - t0) > 2.0
        if not ctx["host"] and out["from"] is not None and ctx["since"] < out["from"]:
            out["state"] = "rejoin-needed"
        elif (not ctx["host"] and out["host_made"] is not None
              and (time.time() - out["host_made"]) > SILENT_DAYS * 86400):
            out["state"] = "host-silent"
        return out

    def _apply(self, result, build_next=True):
        """Main thread: open scanned data files, update store and status, build next writes."""
        st = self.node.store
        rep = {
            "packs": 0,
            "in": 0,
            "applied": 0,
            "carried": 0,
            "waiting": 0,
            "dups": 0,
            "refused": list(result.get("refused") or []),
            "conflicts": 0,
            "skipped": result.get("skipped", 0),
            "devices": len(result.get("folders") or []),
            "pending": result.get("pending", 0),
        }
        if result.get("state") == "error":
            self.status.update(state="error", error=result.get("error", ""), last=time.time())
            return None

        self._marker_ok = result.get("state") not in ("no-drive", "missing", "no-marker")
        done = set(st.get("folder_done") or [])
        head_seen = dict(st.get("folder_head_seen") or {})
        for f in result.get("folders") or []:
            maker = f["maker"]
            if f["n"] > int(head_seen.get(maker, 0)):
                head_seen[maker] = f["n"]

        if result["state"] != "rejoin-needed":
            for f in result.get("folders") or []:
                entry = f["entry"]
                for name, sha, data in f["files"]:
                    key = entry + "/" + name + ":" + sha
                    if key in done:
                        continue
                    try:
                        r = pigeon.open_data(self.node, data)
                    except pigeon.PigeonError as e:
                        rep["refused"].append(str(e))
                        done.add(key)
                        continue
                    for k in ("packs", "in", "applied", "dups", "conflicts"):
                        rep[k] += r.get(k, 0)
                    rep["refused"] += r.get("refused", [])
                    rep["waiting"] = r.get("waiting", 0)
                    if "expired" not in (r.get("refused") or []):
                        if not _is_snap(name) or int(r.get("waiting", 0)) == 0:
                            done.add(key)

        st.put("folder_done", sorted(done)[-KEEP_DONE:])
        st.put("folder_head_seen", head_seen)
        self.status.update(
            last=time.time(),
            devices=rep["devices"],
            pending=rep["pending"],
            read=self.status["read"] + rep["packs"],
            refused=self.status["refused"] + len(rep["refused"]),
            error="",
            state=result["state"],
            host_made=result.get("host_made"),
            heads=result.get("heads") or {},
            streamed=bool(result.get("streamed")),
            placeholders=result.get("placeholders", 0),
            slow=bool(result.get("slow")),
            unreadable=result.get("unreadable", 0),
        )
        self.status["from"] = result.get("from")

        if result["state"] in ("no-drive", "missing"):
            self._miss_scans += 1
            if self._miss_scans % 4 == 0:
                from ..field_photos import drive

                roster = _node_roster(self.node) or None
                hits = cloudfs.find_room(self.node.keys.tag, drive.suggest_roots())
                valid = [
                    h for h in hits
                    if read_marker(room_dir(h, self.node), self.node.keys.key, self.node.keys.tag, roster) is not None
                ]
                if len(valid) == 1:
                    self.folder = valid[0]
                    st.put("folder_carrier", valid[0])
                    self._miss_scans = 0
        else:
            self._miss_scans = 0

        if build_next:
            self._pending = self._build_writes(force=False)
        return rep

    # ---- the loop -------------------------------------------------------------------------------------------
    def sync(self, force=False):
        try:
            with self._io_lock:
                writes, deletes, meta = self._pending
                self._pending = ([], [], {})
                res = self._scan(self._ctx(), writes, deletes)
            if res.get("wrote_ok") and meta:
                self._commit_meta(meta)
            rep = self._apply(res, build_next=False)
            self.write(force=force)
            return rep
        except OSError as e:
            self.status["error"] = str(e)
            self.status["state"] = "error"
            return None

    def _run_scan(self, ctx, writes, deletes):
        try:
            with self._io_lock:
                res = self._scan(ctx, writes, deletes)
        except Exception as e:  # noqa: BLE001
            res = {
                "state": "error",
                "error": str(e),
                "folders": [],
                "pending": 0,
                "skipped": 0,
                "refused": [],
                "placeholders": 0,
                "slow": False,
                "unreadable": 0,
                "streamed": False,
                "host_made": None,
                "heads": {},
                "from": None,
                "wrote_ok": False,
                "wrote_n": 0,
            }
        self._q.put(res)

    def _kick_scan(self):
        if self._scan_thread is not None and self._scan_thread.is_alive():
            return
        self._last_scan = time.monotonic()
        ctx = self._ctx()
        writes, deletes, meta = self._pending
        if not writes and not deletes and self._marker_ok is not None:   # the first scan checks the marker
            writes, deletes, meta = self._build_writes(force=False)
        self._pending = ([], [], {})
        self._inflight_meta = meta
        self._scan_thread = threading.Thread(
            target=self._run_scan, args=(ctx, writes, deletes), name="biruni-room-folder", daemon=True
        )
        self._scan_thread.start()

    def _tick(self):
        while True:
            try:
                res = self._q.get_nowait()
            except queue.Empty:
                break
            if res.get("wrote_ok") and self._inflight_meta:
                self._commit_meta(self._inflight_meta)
            self._inflight_meta = {}
            self._apply(res, build_next=True)
        if self._scan_thread is None or not self._scan_thread.is_alive():
            if time.monotonic() - self._last_scan >= self._interval:
                self._kick_scan()

    def start(self, interval=INTERVAL):
        from qgis.PyQt.QtCore import QTimer

        self.stop()
        self._interval = interval
        self._q = queue.Queue()
        self._stop_ev.clear()
        self._last_scan = 0.0
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)
        self._kick_scan()

    def stop(self):
        if self.timer is not None:
            self.timer.stop()
            self.timer = None
        self._stop_ev.set()
        if self._scan_thread is not None and self._scan_thread.is_alive():
            self._scan_thread.join(timeout=5)
        self._scan_thread = None


# ---- the room's folder, remembered in its record ----------------------------------------------------------------
_CARRIER = [None]


def current():
    return _CARRIER[0]


def use_folder(node, folder):
    """Carry this room through folder from now on (None: stop). -> the carrier or None."""
    old = _CARRIER[0]
    if old is not None:
        old.stop()
        _CARRIER[0] = None
    if not folder:
        node.store.put("folder_carrier", "")
        return None
    tag = node.keys.tag
    base = room_dir(folder, node)
    roster = _node_roster(node)
    if pigeon._role(node) == "host":
        made = node.store.get("folder_made")
        if not os.path.isdir(base) and made != tag:
            create_room_folder(node, folder)
        elif os.path.isdir(base) and made != tag:
            if read_marker(base, node.keys.key, tag, roster or None) is not None:
                node.store.put("folder_made", tag)
            elif os.path.isdir(os.path.join(base, "d-" + node.me_id)):
                create_room_folder(node, folder)
    else:
        if not os.path.isdir(base) or read_marker(base, node.keys.key, tag, roster or None) is None:
            raise FolderError("no-room-here")
    node.store.put("folder_carrier", folder)
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
    if folder and _CARRIER[0] is None:
        c = FolderCarrier(node, folder)
        _CARRIER[0] = c
        c.start()
