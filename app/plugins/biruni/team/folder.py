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
- Folder access without the code: sizes, times and ids only. It can delete, hold back or replay, but cannot read,
  forge, or even ask to join.
- Code and folder access, but not approved:
  - it can read the marker, the join requests (names and public keys), the heads and the stream packs, up to the
    last 20,000 changes;
  - it cannot read the base data, whose key is sealed only to approved members' keys;
  - it cannot write, because it is not in the roster.
- So approval protects the base data, and later changes are protected by the code alone. Give the code only to
  people you would approve.
- A removed member keeps what it already received. Change the code, and unshare the folder in Drive.
- The host right is checked by every computer against the owner's signature. A computer that makes itself host
  without it is followed by nobody.
- The marker stops a silent re-creation, not a deletion.
"""
import hashlib
import io
import json
import os
import queue
import re
import threading
import time
import uuid
import weakref
import zipfile

from ..pigeon import core as pigeon
from ..secure import crypto
from . import cloudfs, rules, snapshot

HEAD = "head.bph"
EXT = ".bpf"
AAD = b"biruni-room-folder-1"
MARKER_AAD = b"biruni-room-marker-1"
JOIN_AAD = b"biruni-room-join-1"
WELCOME_AAD = b"biruni-room-welcome-1|"
BASE_AAD = b"biruni-room-base-1|"
REKEY_AAD = b"biruni-room-rekey-1"
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
BASE_PART = 8 << 20               # bytes per base package chunk before sealing
MAX_BASE = 2 << 30                # maximum total size of a base package (2 GB)
MAX_JOIN_READ = 64 << 10          # maximum bytes read from a join request (.bpj)
MAX_JOIN_DIRS = 50                # maximum non-roster d- folders scanned for join requests
BASE_REUSE_HEAD = 500             # reuse a base package while head - pkg.head < 500
BASE_REUSE_AGE = 7 * 86400        # ... and while it is less than 7 days old
REKEY_KEEP_DAYS = 90              # days a rekey notice stays in the old room folder

_NAME = re.compile(r"^\d{6}-[0-9a-f]{12}\.bpf$")
_HEAD_RE = re.compile(r"^head-(\d{6})\.bph$")
_SNAP_RE = re.compile(r"^snap-(\d{8})-(\d{2})\.bpf$")
_MARKER_RE = re.compile(r"^room-(\d{4})\.bpr$")
_JOIN_RE = re.compile(r"^join-(\d{6})\.bpj$")
_WELCOME_RE = re.compile(r"^welcome-([0-9a-f]{16})-(\d{4})\.bpw$")
_BASE_RE = re.compile(r"^base-([0-9a-f]{12})-(\d{4})\.bpb$")
_REKEY_RE = re.compile(r"^rekey-(\d+)\.bpk$")


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


class _MemStore:
    """Read-only dict wrapper passed to rules.may_host inside the worker thread."""

    def __init__(self, data):
        self._d = data

    def get(self, k, default=None):
        return self._d.get(k, default)


def read_marker(base, key, tag, roster=None, store=None, lookup=None, current_epoch=None, refused=None):
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
        maker = str(body.get("maker") or "")
        if roster is not None:
            who = next((m for m in roster if m.get("id") == maker), None)
            if who is None or who.get("state") == "out" or not who.get("pub") or who["pub"] != body.get("pub"):
                continue
        if store is not None and lookup is not None and current_epoch is not None and ep > int(current_epoch):
            if not rules.may_host(store, lookup, maker):
                if refused is not None:
                    refused.append(maker)
                continue
        return body
    return None


def _write_marker_file(node, base, at=0, prev_sid=None):
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
        "at": int(at),
        "prev_sid": prev_sid,
    }
    blob = crypto.seal(node.keys.key, crypto.canonical(pigeon._sign(node, body)), MARKER_AAD)
    _write_once(os.path.join(base, _marker_name(epoch)), blob)
    node.store.put("folder_made", node.keys.tag)


def create_room_folder(node, parent):
    """Host only: create the room directory under ``parent`` and write the room marker."""
    if pigeon._role(node) != "host":
        raise FolderError("not-host")
    if not parent or not os.path.isdir(parent):
        raise FolderError("no-parent")
    base = room_dir(parent, node)
    if not os.path.isdir(base):
        os.mkdir(base)
    _write_marker_file(node, base, at=0, prev_sid=None)
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


def _pick_head(d, entries, key, tag, roster, head_seen=None, check_placeholder=True, trusted_pub=None):
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
        if roster:
            who = next((m for m in roster if m.get("id") == maker), None)
            if who is None or not who.get("pub") or who["pub"] != body.get("pub") or who.get("state") == "out":
                last_err = "not a member"
                continue
        elif trusted_pub:
            if body.get("pub") != trusted_pub:
                last_err = "not the host"
                continue
        else:
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


_read_head = _pick_head


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
    """Lowest ``after`` contiguously reaching ``head`` across the pack files in ``files``."""
    packs = [k for k in files if _is_snap(k) or _NAME.match(k)]
    if not packs:
        return max(0, head - pigeon.DOWN_KEEP)
    intervals = sorted(
        ((int(file_after.get(k, 0)), int(file_hi.get(k, head))) for k in packs),
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


def _unique_data_dir(room_folder):
    data_root = os.path.join(room_folder, "data")
    os.makedirs(data_root, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    cand, n = os.path.join(data_root, stamp), 2
    while os.path.exists(cand):
        cand, n = os.path.join(data_root, f"{stamp}-{n}"), n + 1
    os.makedirs(cand, exist_ok=True)
    return cand


def write_join_request(base, keys, ident, rebase=False):
    """Write ``d-<id>/join-<n:06d>.bpj`` inside a room folder that has a valid marker. -> (path, n)."""
    if not base or not os.path.isdir(base) or read_marker(base, keys.key, keys.tag) is None:
        raise FolderError("no-room-here")
    from . import wire

    my_dir = os.path.join(base, "d-" + ident["id"])
    if not os.path.isdir(my_dir):
        os.mkdir(my_dir)
    existing = []
    for fname in os.listdir(my_dir):
        m = _JOIN_RE.match(fname)
        if m:
            existing.append((int(m.group(1)), fname))
    existing.sort(key=lambda x: -x[0])
    n = (existing[0][0] + 1) if existing else 1
    pub = crypto.public_key(bytes.fromhex(ident["seed"])).hex()
    _bp, xpub = wire.box_key(ident["seed"])
    body = {
        "v": 1,
        "kind": "join",
        "room": keys.tag,
        "maker": ident["id"],
        "name": ident.get("name", ""),
        "pub": pub,
        "xpub": xpub,
        "made": time.time(),
        "n": n,
    }
    if rebase:
        body["rebase"] = True
    signed = dict(body)
    signed["sig"] = crypto.sign(bytes.fromhex(ident["seed"]), crypto.canonical(signed)).hex()
    blob = crypto.seal(keys.key, crypto.canonical(signed), JOIN_AAD)
    fname = f"join-{n:06d}.bpj"
    path = os.path.join(my_dir, fname)
    _write_once(path, blob)
    for _, old_name in existing[1:]:
        try:
            os.remove(os.path.join(my_dir, old_name))
        except OSError:
            pass
    return path, n


def unseal_welcome(data, box_priv, joiner_id, marker_pub, marker_maker, tag):
    """Unseal and verify a ``welcome-*.bpw`` payload against ``marker_pub`` and ``marker_maker``."""
    w = json.loads(bytes(data).decode("utf-8"))
    raw = crypto.box_open(box_priv, bytes.fromhex(w["eph"]), bytes.fromhex(w["blob"]),
                          WELCOME_AAD + str(joiner_id).encode("utf-8"))
    body = json.loads(raw.decode("utf-8"))
    if not isinstance(body, dict) or body.get("v") != 1 or body.get("kind") not in ("welcome", "refused"):
        raise ValueError("not a welcome")
    if body.get("room") != tag or body.get("maker") != marker_maker or body.get("for") != joiner_id:
        raise ValueError("welcome mismatch")
    if body.get("pub") != marker_pub or not pigeon._signed_ok(body):
        raise ValueError("bad welcome signature")
    return body


def extract_base(host_dir, base_info, dest_folder, report=lambda *a: None, parts_data=None):
    """Verify SHA, unseal, and extract base package parts into ``dest_folder`` safely."""
    pkg_id = str(base_info["id"])
    pkg_key = crypto.unb64(base_info["key"])
    parts = list(base_info.get("parts") or [])
    buf = bytearray()
    total_parts = len(parts)
    for i, part in enumerate(parts):
        report("downloading", i + 1, total_parts)
        pname = part["name"]
        if parts_data is not None and pname in parts_data:
            blob = parts_data[pname]
        else:
            fpath = os.path.join(host_dir, pname)
            blob = _read(fpath)
        if len(blob) > MAX_READ or _sha(blob) != part["sha"]:
            raise ValueError(f"damaged base package part: {pname}")
        aad = (BASE_AAD.decode("ascii") + f"{pkg_id}|{i}").encode("utf-8")
        chunk = crypto.unseal(pkg_key, blob, aad, max_size=MAX_READ)
        buf.extend(chunk)
        if len(buf) > MAX_BASE:
            raise ValueError("base package too large")
    os.makedirs(dest_folder, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(bytes(buf)), "r") as zf:
        total_out = 0
        items = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            dest = snapshot.inside(dest_folder, info.filename)
            if dest is None:
                raise ValueError(f"unsafe file name in base package: {info.filename!r}")
            total_out += max(0, int(info.file_size))
            if total_out > MAX_BASE:
                raise ValueError("base package too large")
            items.append((info, dest))
        for info, dest in items:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info, "r") as src, open(dest, "wb") as dst:
                dst.write(src.read())
    with open(os.path.join(dest_folder, "versions.json"), encoding="utf-8") as f:
        v = json.load(f)
    files = {k: n for k, n in (v.get("layer_files") or {}).items() if snapshot.safe_relative(n)}
    extra = {k: n for k, n in (v.get("extra") or {}).items() if snapshot.safe_relative(n)}
    return {"folder": dest_folder, "head": v["head"], "versions": v["versions"], "layer_files": files, "extra": extra}


def _scan_welcome_and_base(hd, fmap, me_id, box_priv, marker, tag, out, min_made=0.0):
    """Worker-thread helper: read and verify newest welcome + its base parts from ``hd``."""
    prefix = me_id[:16]
    cands = []
    for fname in fmap:
        m = _WELCOME_RE.match(fname)
        if m and m.group(1) == prefix:
            cands.append((int(m.group(2)), fname))
    cands.sort(key=lambda x: -x[0])
    for _wn, wname in cands:
        wsha = fmap[wname]
        wpath = os.path.join(hd, wname)
        if cloudfs.placeholder(wpath) is True:
            out["placeholders"] += 1
            out["pending"] += 1
            continue
        try:
            if os.path.getsize(wpath) > MAX_READ:
                continue
            wblob = _read(wpath)
        except OSError:
            out["unreadable"] += 1
            out["pending"] += 1
            continue
        if len(wblob) > MAX_READ or _sha(wblob) != wsha:
            out["pending"] += 1
            continue
        try:
            wbody = unseal_welcome(wblob, box_priv, me_id, marker["pub"], marker["maker"], tag)
        except Exception:  # noqa: BLE001
            continue
        if float(wbody.get("made") or 0) <= min_made:
            continue
        if wbody.get("kind") != "welcome" or not wbody.get("base"):
            continue
        parts_data = {}
        ready = True
        for part in wbody["base"].get("parts") or []:
            pname = part["name"]
            if fmap.get(pname) != part["sha"]:
                out["pending"] += 1
                ready = False
                break
            ppath = os.path.join(hd, pname)
            if cloudfs.placeholder(ppath) is True:
                out["placeholders"] += 1
                out["pending"] += 1
                ready = False
                break
            try:
                if os.path.getsize(ppath) > MAX_READ:
                    ready = False
                    break
                pdata = _read(ppath)
            except OSError:
                out["unreadable"] += 1
                out["pending"] += 1
                ready = False
                break
            if len(pdata) > MAX_READ or _sha(pdata) != part["sha"]:
                out["pending"] += 1
                ready = False
                break
            parts_data[pname] = pdata
        if ready:
            return {"key": os.path.basename(hd) + "/" + wname + ":" + wsha,
                    "marker": marker, "welcome": wbody, "parts": parts_data}
    return None


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
        self._waiting_joins = {}     # cid -> request dict awaiting host decision
        self._pending_welcomes = []  # [(cid, xpub, kind, reason, member_dict)]
        self._need_rebase_join = False
        self._base_too_large_flag = False
        _CARRIERS[node] = self
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
            "refused_host": "",
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

    def write_marker(self, at=0, prev_sid=None):
        """Write a new room-<epoch:04d>.bpr marker in ``self.base`` for this host."""
        if pigeon._role(self.node) != "host":
            raise FolderError("not-host")
        if not os.path.isdir(self.base):
            raise FolderError("missing")
        with self._io_lock:
            _write_marker_file(self.node, self.base, at=at, prev_sid=prev_sid)
        self._marker_ok = True

    def write_takeover_welcomes(self):
        """Queue takeover welcomes for every active roster member with an xpub and write now."""
        if pigeon._role(self.node) != "host" or self.node.hub is None:
            return 0
        for m in self.node.hub.roster():
            if m.get("id") == self.node.me_id or m.get("state") == "out":
                continue
            xpub = str(m.get("xpub") or "")
            if len(xpub) == 64:
                mdict = {k: m.get(k) for k in ("id", "name", "rank", "rights")}
                self._pending_welcomes.append((m["id"], xpub, "welcome", "takeover", mdict))
        return self.write(force=True)

    def write_rekey(self, rekey):
        """Write ``rekey-<int(at)>.bpk`` under the old room tag and key before switching codes."""
        if not rekey or pigeon._role(self.node) != "host" or not os.path.isdir(self.base):
            return 0
        node = self.node
        st = node.store
        roster = _node_roster(node)
        with self._io_lock:
            if read_marker(self.base, node.keys.key, node.keys.tag, roster or None) is None:
                return 0
            my_dir = self.my_dir()
            files = self._my_files()
            now = time.time()
            body = {
                "v": 1,
                "kind": "rekey",
                "room": node.keys.tag,
                "maker": node.me_id,
                "made": now,
                "at": float(rekey["at"]),
                "boxes": rekey["boxes"],
            }
            blob = crypto.seal(node.keys.key, crypto.canonical(pigeon._sign(node, body)), REKEY_AAD, compress=True)
            fname = f"rekey-{int(rekey['at'])}.bpk"
            files[fname] = _sha(blob)
            file_made = dict(st.get("folder_file_made") or {})
            file_made[fname] = now
            file_hi = dict(st.get("folder_file_hi") or {})
            file_after = dict(st.get("folder_file_after") or {})
            from_seq = _from_seq(files, file_after, file_hi, st.head())
            n = int(st.get("folder_head_n") or 0) + 1
            head_name = f"head-{n:06d}.bph"
            head_body = pigeon._sign(node, self._head_body(files, n, from_seq=from_seq))
            head_blob = crypto.seal(node.keys.key, crypto.canonical(head_body), AAD, compress=True)
            writes = [(os.path.join(my_dir, fname), blob, False), (os.path.join(my_dir, head_name), head_blob, True)]
            deletes = [d for d in self._old_head_deletes(head_name) if d not in {w[0] for w in writes}]
            _perform_writes(my_dir, writes, deletes)
        st.put("folder_file_made", {k: v for k, v in file_made.items() if k in files})
        st.put("folder_head_n", n)
        st.put("folder_head_at", now)
        st.put("folder_written_at", now)
        self.status["written"] += 1
        return 1

    def members_view(self):
        """Per-member sync progress on the host: [{"id", "name", "seen", "head", "last"}]."""
        st = self.node.store
        head = int(st.head())
        seen = st.get("pigeon_seen_map") or {}
        heads = self.status.get("heads") or {}
        out = []
        for m in _node_roster(self.node):
            mid = m.get("id")
            if not mid or mid == self.node.me_id or m.get("state") == "out" or m.get("host"):
                continue
            out.append({
                "id": mid,
                "name": m.get("name", ""),
                "seen": int(seen.get(mid, 0)),
                "head": head,
                "last": heads.get(mid),
            })
        return out

    def _base_too_large(self):
        self._base_too_large_flag = True
        self.status["state"] = "base-too-large"
        self.node.status.emit("warn", "base-too-large")

    def _ensure_base_pkg(self, files, writes, my_dir, now, meta):
        """Reuse an existing valid base package or build, zip, split and seal a new one."""
        st = self.node.store
        head = st.head()
        pkg = st.get("folder_base_pkg")
        if (pkg and (head - int(pkg.get("head") or 0)) < BASE_REUSE_HEAD
                and (now - float(pkg.get("made") or 0)) < BASE_REUSE_AGE
                and all(p["name"] in files and os.path.isfile(os.path.join(my_dir, p["name"]))
                        for p in (pkg.get("parts") or []))):
            self._base_too_large_flag = False
            return dict(pkg, _fresh=False)
        if self.node.hub is None:
            return None
        base_info = self.node.hub.make_base()
        if sum((base_info.get("files") or {}).values()) > MAX_BASE:
            self._base_too_large()
            return None
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root_d, _dirs, fnames in os.walk(base_info["dir"]):
                for fn in sorted(fnames):
                    full = os.path.join(root_d, fn)
                    rel = os.path.relpath(full, base_info["dir"]).replace("\\", "/")
                    zf.write(full, rel)
        raw_zip = buf.getvalue()
        if len(raw_zip) > MAX_BASE:
            self._base_too_large()
            return None
        self._base_too_large_flag = False
        if pkg:
            old_pkgs = list(st.get("folder_old_base_pkgs") or [])
            old_pkgs.append(pkg)
            meta["folder_old_base_pkgs"] = old_pkgs
        pkg_id = uuid.uuid4().hex[:12]
        pkg_key = crypto.random_key()
        parts = []
        for i in range(0, max(1, len(raw_zip)), BASE_PART):
            chunk = raw_zip[i:i + BASE_PART]
            idx = i // BASE_PART
            aad = (BASE_AAD.decode("ascii") + f"{pkg_id}|{idx}").encode("utf-8")
            sealed = crypto.seal(pkg_key, chunk, aad)
            pname = f"base-{pkg_id}-{idx:04d}.bpb"
            writes.append((os.path.join(my_dir, pname), sealed, False))
            files[pname] = _sha(sealed)
            parts.append({"name": pname, "sha": files[pname], "size": len(sealed)})
        new_pkg = {
            "id": pkg_id,
            "key": crypto.b64(pkg_key),
            "head": base_info["head"],
            "made": now,
            "parts": parts,
            "left_out": base_info["left_out"],
            "welcomed": [],
            "_fresh": True,
        }
        return new_pkg

    def _make_welcome_blob(self, joiner_id, xpub, kind, reason, member_dict, pkg):
        node = self.node
        body = {
            "v": 1,
            "kind": kind,
            "reason": reason,
            "room": node.keys.tag,
            "maker": node.me_id,
            "for": joiner_id,
            "made": time.time(),
            "member": member_dict,
            "room_info": node.hub.room_info() if kind == "welcome" and node.hub is not None else None,
            "base": ({"id": pkg["id"], "key": pkg["key"], "head": pkg["head"],
                      "parts": pkg["parts"], "left_out": pkg["left_out"]}
                     if kind == "welcome" and pkg is not None else None),
        }
        signed = pigeon._sign(node, body)
        eph, blob = crypto.box_seal(bytes.fromhex(xpub), crypto.canonical(signed),
                                    WELCOME_AAD + str(joiner_id).encode("utf-8"))
        return crypto.canonical({"eph": eph.hex(), "blob": blob.hex()})

    def _collect_waiting_joins(self, meta):
        if self.node.hub is None or not self._waiting_joins:
            return
        st = self.node.store
        join_seen = dict(meta.get("folder_join_seen") or st.get("folder_join_seen") or {})
        changed = False
        for cid, req in list(self._waiting_joins.items()):
            res = self.node.hub.folder_join(req)
            if res == "ok":
                self._waiting_joins.pop(cid, None)
                m = next((x for x in self.node.hub.store.members() if x["id"] == cid), None)
                if m is not None:
                    mdict = {k: m.get(k) for k in ("id", "name", "rank", "rights")}
                    self._pending_welcomes.append((cid, req["xpub"], "welcome", "join", mdict))
                join_seen[cid] = max(int(join_seen.get(cid, 0)), int(req.get("n") or 0))
                changed = True
            elif res == "refused":
                self._waiting_joins.pop(cid, None)
                self._pending_welcomes.append((cid, req["xpub"], "refused", "join", None))
                join_seen[cid] = max(int(join_seen.get(cid, 0)), int(req.get("n") or 0))
                changed = True
        if changed:
            meta["folder_join_seen"] = join_seen

    def _cleanup_base_pkgs(self, files, deletes, file_made, seen, now, meta):
        st = self.node.store
        pkg = meta.get("folder_base_pkg", st.get("folder_base_pkg"))
        old_pkgs = list(meta.get("folder_old_base_pkgs", st.get("folder_old_base_pkgs") or []))
        removed = False
        kept_old = []
        for p in old_pkgs:
            welcomed = p.get("welcomed") or []
            all_seen = bool(welcomed) and all(m in seen for m in welcomed)
            too_old = (now - float(p.get("made") or 0)) >= self.keep_days * 86400
            if all_seen or too_old:
                for part in p.get("parts") or []:
                    if files.pop(part["name"], None) is not None:
                        deletes.append(os.path.join(self.my_dir(), part["name"]))
                        removed = True
            else:
                kept_old.append(p)
        if kept_old != (st.get("folder_old_base_pkgs") or []):
            meta["folder_old_base_pkgs"] = kept_old

        if pkg and not pkg.get("_fresh"):
            welcomed = pkg.get("welcomed") or []
            all_seen = bool(welcomed) and all(m in seen for m in welcomed)
            too_old = (now - float(pkg.get("made") or 0)) >= self.keep_days * 86400
            if all_seen or too_old:
                for part in pkg.get("parts") or []:
                    if files.pop(part["name"], None) is not None:
                        deletes.append(os.path.join(self.my_dir(), part["name"]))
                        removed = True
                for k in list(files):
                    m = _WELCOME_RE.match(k)
                    if m and any(w[:16] == m.group(1) for w in welcomed):
                        files.pop(k, None)
                        deletes.append(os.path.join(self.my_dir(), k))
                        removed = True
                meta["folder_base_pkg"] = None
        # Drop stale welcome-*.bpw and expired rekey-*.bpk files.
        for k in list(files):
            if _WELCOME_RE.match(k):
                made = file_made.get(k, now)
                if (now - float(made)) >= self.keep_days * 86400:
                    files.pop(k, None)
                    deletes.append(os.path.join(self.my_dir(), k))
                    removed = True
            elif _REKEY_RE.match(k):
                made = file_made.get(k, now)
                if (now - float(made)) >= REKEY_KEEP_DAYS * 86400:
                    files.pop(k, None)
                    deletes.append(os.path.join(self.my_dir(), k))
                    removed = True
        return removed

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
        meta = {}
        if host:
            self._collect_waiting_joins(meta)
        urgent = bool(self._pending_welcomes) if host else bool(self._need_rebase_join)
        last_at = st.get("folder_written_at")
        if not force and not urgent and last_at is not None and now - float(last_at) < WRITE_EVERY:
            return [], [], {}

        my_dir = self.my_dir()
        files = self._my_files()
        writes = []
        deletes = []
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
            file_hi = dict(st.get("folder_file_hi") or {})
            file_after = dict(st.get("folder_file_after") or {})
            file_made = dict(st.get("folder_file_made") or {})
            seen = st.get("pigeon_seen_map") or {}

            # Drop any leftover member up-packs from before a takeover.
            for k in list(files):
                if _NAME.match(k) and k not in file_hi:
                    files.pop(k, None)
                    deletes.append(os.path.join(my_dir, k))
                    need_head = True

            cleaned = self._cleanup_base_pkgs(files, deletes, file_made, seen, now, meta)
            need_welcomes = bool(self._pending_welcomes)
            if not (need_incr or need_snap or need_head or need_welcomes or cleaned):
                return [], [], {}

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

            if need_welcomes:
                pending = list(self._pending_welcomes)
                self._pending_welcomes = []
                need_pkg = any(kind == "welcome" for _, _, kind, _, _ in pending)
                pkg = self._ensure_base_pkg(files, writes, my_dir, now, meta) if need_pkg else None
                welcome_n = dict(st.get("folder_welcome_n") or {})
                for jid, xpub, kind, reason, mdict in pending:
                    if kind == "welcome" and pkg is None:
                        continue
                    wn = int(welcome_n.get(jid, 0)) + 1
                    welcome_n[jid] = wn
                    wname = f"welcome-{jid[:16]}-{wn:04d}.bpw"
                    wblob = self._make_welcome_blob(jid, xpub, kind, reason, mdict, pkg)
                    writes.append((os.path.join(my_dir, wname), wblob, False))
                    files[wname] = _sha(wblob)
                    file_made[wname] = now
                    if kind == "welcome" and pkg is not None and jid not in pkg["welcomed"]:
                        pkg["welcomed"].append(jid)
                if pkg is not None:
                    pkg.pop("_fresh", None)
                    meta["folder_base_pkg"] = pkg
                meta["folder_welcome_n"] = welcome_n

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
        if self._need_rebase_join:
            last_req = st.get("folder_rebase_join_at")
            if force or last_req is None or (now - float(last_req)) >= REFRESH:
                self._need_rebase_join = False
                write_join_request(self.base, node.keys, node.ident, rebase=True)
                st.put("folder_rebase_join_at", now)
                return [], [], {}

        uids = sorted(o.get("uid") for o in st.outbox(1 << 30))
        key = _sha(json.dumps([uids, int(node.since or 0)]).encode())
        last_key = st.get("folder_written_key")
        age = (now - float(last_at)) if last_at is not None else None
        want_change = key != last_key and (bool(uids) or age is None or age > UP_EVERY)
        want_refresh = bool(uids) and age is not None and age >= REFRESH
        if not (want_change or want_refresh):
            return [], [], {}

        st.put("pigeon_only", True)     # the host is far, not gone: never take the room over for it
        # Drop any leftover host files if this computer stepped down from host.
        for k in list(files):
            if _is_snap(k) or _BASE_RE.match(k) or _WELCOME_RE.match(k) or _REKEY_RE.match(k):
                files.pop(k, None)
                deletes.append(os.path.join(my_dir, k))
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

    def _write_up_ops(self, ops):
        """Write an explicit list of ops as up packs (used after takeover rebase)."""
        node = self.node
        st = node.store
        my_dir = self.my_dir()
        files = self._my_files()
        deletes = []
        for k in list(files):
            if _is_snap(k) or _BASE_RE.match(k) or _WELCOME_RE.match(k) or _REKEY_RE.match(k) or _NAME.match(k):
                files.pop(k, None)
                deletes.append(os.path.join(my_dir, k))
        seq = 1 + max([int(k[:6]) for k in files if _NAME.match(k)] + [0])
        now = time.time()
        out_packs = []
        for part in pigeon._parts(ops):
            b = pigeon._base(node, "up")
            b.update(ops=part, since=node.since)
            out_packs.append(b)
        blobs = [pigeon.seal(node, b) for b in out_packs]
        writes = []
        for data in pigeon.envelopes(blobs, node.keys.tag):
            name = f"{seq:06d}-{_sha(data)[:12]}{EXT}"
            seq += 1
            writes.append((os.path.join(my_dir, name), data, False))
            files[name] = _sha(data)
        n = int(st.get("folder_head_n") or 0) + 1
        head_name = f"head-{n:06d}.bph"
        head_body = pigeon._sign(node, self._head_body(files, n))
        head_blob = crypto.seal(node.keys.key, crypto.canonical(head_body), AAD, compress=True)
        writes.append((os.path.join(my_dir, head_name), head_blob, True))
        deletes.extend(self._old_head_deletes(head_name))
        with self._io_lock:
            _perform_writes(my_dir, writes, deletes)
        st.put("folder_head_n", n)
        st.put("folder_written_at", now)
        uids = sorted(o.get("uid") for o in st.outbox(1 << 30))
        st.put("folder_written_key", _sha(json.dumps([uids, int(node.since or 0)]).encode()))
        self.status["written"] += sum(1 for w in writes if not w[2])

    # ---- reading (worker thread + main thread) --------------------------------------------------------------
    def _ctx(self):
        """Snapshot of state needed by _scan(), taken on the main thread."""
        st = self.node.store
        return {
            "key": self.node.keys.key,
            "tag": self.node.keys.tag,
            "me": self.node.me_id,
            "box_priv": self.node._box_priv,
            "host": pigeon._role(self.node) == "host",
            "roster": _node_roster(self.node),
            "done": set(st.get("folder_done") or []),
            "head_seen": dict(st.get("folder_head_seen") or {}),
            "join_seen": dict(st.get("folder_join_seen") or {}),
            "base": self.base,
            "folder": self.folder,
            "since": int(self.node.since or 0),
            "epoch": int(self.node.epoch or 1),
            "host_grants": list(st.get(rules.HKEY) or []),
            "last_welcome_made": float(st.get("folder_last_welcome_made") or 0.0),
        }

    def _scan_join_request(self, d, d_files, folder_maker, ctx, out):
        cands = []
        for fname in d_files:
            m = _JOIN_RE.match(fname)
            if m:
                cands.append((int(m.group(1)), fname))
        cands.sort(key=lambda x: -x[0])
        if not cands:
            return None
        file_n, fname = cands[0]
        if file_n <= int(ctx["join_seen"].get(folder_maker, 0)):
            return None
        fpath = os.path.join(d, fname)
        if cloudfs.placeholder(fpath) is True:
            out["placeholders"] += 1
            out["pending"] += 1
            return None
        try:
            if os.path.getsize(fpath) > MAX_JOIN_READ:
                return None
            blob = _read(fpath)
        except OSError:
            out["unreadable"] += 1
            return None
        if len(blob) > MAX_JOIN_READ:
            return None
        try:
            body = json.loads(crypto.unseal(ctx["key"], blob, JOIN_AAD, max_size=MAX_JOIN_READ).decode("utf-8"))
        except (crypto.CryptoError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(body, dict) or body.get("kind") != "join" or body.get("room") != ctx["tag"]:
            return None
        if body.get("maker") != folder_maker or body.get("n") != file_n:
            return None
        if len(str(body.get("xpub") or "")) != 64 or not pigeon._signed_ok(body):
            return None
        return body

    def _scan(self, ctx, writes, deletes):
        """Worker thread: write, delete, verify heads, and read files. Never touches node, store or Qt."""
        folder = ctx["folder"]
        base = ctx["base"]
        streamed = (cloudfs.kind(folder) == "gdrive-stream") if folder else False
        out = {
            "state": "ok",
            "folders": [],
            "joins": [],
            "rekeys": [],
            "takeover": None,
            "rebase_welcome": None,
            "refused_host": "",
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
        refused_hosts = []
        mem_store = _MemStore({rules.HKEY: ctx["host_grants"]})
        marker = read_marker(
            base, ctx["key"], ctx["tag"], roster or None,
            store=mem_store,
            lookup=lambda cid: next((m for m in roster if m.get("id") == cid), None),
            current_epoch=ctx["epoch"],
            refused=refused_hosts,
        )
        if refused_hosts:
            out["refused_host"] = refused_hosts[0]
        if marker is None:
            out["state"] = "no-marker"
            return out

        if int(marker.get("epoch", 1)) > ctx["epoch"] and marker.get("maker") != ctx["me"]:
            hd = os.path.join(base, "d-" + marker["maker"])
            if os.path.isdir(hd):
                try:
                    d_files = os.listdir(hd)
                except OSError:
                    out["unreadable"] += 1
                    d_files = []
                _hn, _fn, hbody, h_ph, h_unr, _err, _st = _pick_head(
                    hd, d_files, ctx["key"], ctx["tag"], roster, head_seen=None,
                    check_placeholder=True, trusted_pub=marker.get("pub"),
                )
                out["placeholders"] += h_ph
                out["unreadable"] += h_unr
                out["pending"] += h_ph
                if hbody is not None:
                    fmap = {str(k): str(v) for k, v in (hbody.get("files") or {}).items() if isinstance(k, str)}
                    tk = _scan_welcome_and_base(hd, fmap, ctx["me"], ctx["box_priv"], marker, ctx["tag"], out)
                    if tk is not None:
                        out["takeover"] = tk
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

        non_roster_joins = 0
        for entry in entries:
            d = os.path.join(base, entry)
            if not entry.startswith("d-") or not os.path.isdir(d) or entry == "d-" + ctx["me"]:
                continue
            try:
                d_files = os.listdir(d)
            except OSError:
                out["unreadable"] += 1
                continue
            folder_maker = entry[2:]
            member_rec = next((m for m in roster if m.get("id") == folder_maker), None)
            if ctx["host"]:
                if member_rec is None:
                    if non_roster_joins < MAX_JOIN_DIRS:
                        non_roster_joins += 1
                        req = self._scan_join_request(d, d_files, folder_maker, ctx, out)
                        if req is not None:
                            out["joins"].append(req)
                    continue
                if member_rec.get("state") != "out":
                    req = self._scan_join_request(d, d_files, folder_maker, ctx, out)
                    if req is not None:
                        out["joins"].append(req)

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

            if not ctx["host"] and who.get("host"):
                for rname in sorted(k for k in files_map if _REKEY_RE.match(k)):
                    rsha = files_map[rname]
                    rkey = entry + "/" + rname + ":" + rsha
                    if rkey in ctx["done"]:
                        continue
                    rpath = os.path.join(d, rname)
                    if cloudfs.placeholder(rpath) is True:
                        out["placeholders"] += 1
                        out["pending"] += 1
                        continue
                    try:
                        if os.path.getsize(rpath) > MAX_READ:
                            continue
                        rblob = _read(rpath)
                    except OSError:
                        out["unreadable"] += 1
                        out["pending"] += 1
                        continue
                    if len(rblob) > MAX_READ or _sha(rblob) != rsha:
                        out["pending"] += 1
                        continue
                    try:
                        rbody = json.loads(crypto.unseal(ctx["key"], rblob, REKEY_AAD, max_size=8 << 20).decode("utf-8"))
                    except (crypto.CryptoError, ValueError, UnicodeDecodeError):
                        continue
                    if (isinstance(rbody, dict) and rbody.get("kind") == "rekey"
                            and rbody.get("room") == ctx["tag"] and rbody.get("maker") == maker
                            and rbody.get("pub") == who.get("pub") and pigeon._signed_ok(rbody)):
                        out["rekeys"].append((rkey, rbody))

            if not ctx["host"] and not who.get("host"):
                continue
            if not ctx["host"] and who.get("host") and h_from is not None and ctx["since"] < int(h_from):
                rw = _scan_welcome_and_base(
                    d, files_map, ctx["me"], ctx["box_priv"],
                    {"maker": maker, "pub": who.get("pub")}, ctx["tag"], out,
                    min_made=ctx["last_welcome_made"],
                )
                if rw is not None and rw["welcome"]["base"]["head"] >= int(h_from):
                    out["rebase_welcome"] = rw
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

    def _apply_takeover(self, tk):
        marker = tk["marker"]
        wbody = tk["welcome"]
        parts_data = tk["parts"]
        node = self.node
        st = node.store
        if node.hub is not None or node.role == "host":
            node.stop_network()
        node.role = "member"
        st.put("role", "member")
        st.put("pigeon_only", True)
        node.state = "reconnecting"
        at_seq = int(marker.get("at") or 0)
        replay_ops = []
        seq = at_seq
        while True:
            page = st.since(seq, 2000)
            if not page:
                break
            seq = page[-1]["seq"]
            for e in page:
                if e["kind"] != "field" and ":n:" not in e["uid"]:
                    replay_ops.append({
                        "uid": e["uid"], "layer": e["layer"], "fid": e["fid"], "kind": e["kind"],
                        "base_ver": max(0, (e["ver"] or 1) - 1), "base": e["prev"], "state": e["state"],
                        "author": e["author"], "name": e["name"], "ts": e["ts"], "sig": e["sig"],
                        "src": e.get("src"), "replay": True,
                    })
        outbox_ops = st.outbox(1 << 30)
        with st._lock:
            st.con.execute("DELETE FROM ops WHERE seq>?", (at_seq,))
        dest = _unique_data_dir(node.room_folder())
        snap = extract_base(os.path.join(self.base, "d-" + marker["maker"]), wbody["base"], dest,
                            parts_data=parts_data)
        node.rebase(wbody["room_info"], snap, ())
        node.member = wbody["member"]
        st.put("rank", node.member.get("rank", 99))
        node._apply_rights(node.member.get("rights") or {})
        node._tick.start()
        for k, v in (("folder_done", []), ("folder_written_head", None), ("folder_written_settings", None),
                     ("folder_snap_head", 0), ("folder_snap_at", None), ("folder_head_at", None),
                     ("folder_file_hi", {}), ("folder_file_after", {}), ("folder_file_made", {}),
                     ("folder_base_pkg", None), ("folder_old_base_pkgs", []), ("folder_written_key", None)):
            st.put(k, v)
        st.put("folder_last_welcome_made", float(wbody.get("made") or 0))
        self._write_up_ops(replay_ops + outbox_ops)
        self._marker_ok = True
        self.status.update(state="ok", last=time.time(), error="", refused_host="")
        node.changed.emit()

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
        if result.get("refused_host"):
            self.status["refused_host"] = result["refused_host"]
        if result.get("state") == "error":
            self.status.update(state="error", error=result.get("error", ""), last=time.time())
            return None

        if result.get("takeover") is not None:
            self._apply_takeover(result["takeover"])
            return rep

        self._marker_ok = result.get("state") not in ("no-drive", "missing", "no-marker")
        done = set(st.get("folder_done") or [])
        head_seen = dict(st.get("folder_head_seen") or {})
        for f in result.get("folders") or []:
            maker = f["maker"]
            if f["n"] > int(head_seen.get(maker, 0)):
                head_seen[maker] = f["n"]

        for rkey, rbody in result.get("rekeys") or []:
            done.add(rkey)
            box = (rbody.get("boxes") or {}).get(self.node.me_id)
            if box:
                self.node._on_rekey({"at": rbody["at"], "box": box})

        if pigeon._role(self.node) == "host" and self.node.hub is not None and result.get("joins"):
            join_seen = dict(st.get("folder_join_seen") or {})
            for req in result["joins"]:
                cid = req["maker"]
                join_seen[cid] = max(int(join_seen.get(cid, 0)), int(req.get("n") or 0))
                res = self.node.hub.folder_join(req)
                if res == "ok":
                    self._waiting_joins.pop(cid, None)
                    m = next((x for x in self.node.hub.store.members() if x["id"] == cid), None)
                    mdict = {k: m.get(k) for k in ("id", "name", "rank", "rights")} if m is not None else None
                    if req.get("rebase"):
                        seen = dict(st.get("pigeon_seen_map") or {})
                        seen.pop(cid, None)
                        st.put("pigeon_seen_map", seen)
                    self._pending_welcomes.append((cid, req["xpub"], "welcome", "join", mdict))
                elif res == "waiting":
                    self._waiting_joins[cid] = req
                elif res == "refused":
                    self._waiting_joins.pop(cid, None)
                    self._pending_welcomes.append((cid, req["xpub"], "refused", "join", None))
            st.put("folder_join_seen", join_seen)

        rw = result.get("rebase_welcome")
        if rw is not None:
            wbody = rw["welcome"]
            dest = _unique_data_dir(self.node.room_folder())
            snap = extract_base(os.path.join(self.base, "d-" + rw["marker"]["maker"]), wbody["base"], dest,
                                parts_data=rw["parts"])
            self.node.rebase(wbody["room_info"], snap, ())
            self.node.member = wbody["member"]
            st.put("rank", self.node.member.get("rank", 99))
            self.node._apply_rights(self.node.member.get("rights") or {})
            st.put("folder_last_welcome_made", float(wbody.get("made") or 0))
            st.put("folder_rebase_join_at", None)
            self._need_rebase_join = False
            done.add(rw["key"])
            result["state"] = "ok"
            self.node.changed.emit()
        elif result["state"] == "rejoin-needed":
            self._need_rebase_join = True
        else:
            self._need_rebase_join = False

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
        if self._base_too_large_flag and self.status.get("state") == "ok":
            self.status["state"] = "base-too-large"
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
_CARRIERS = weakref.WeakKeyDictionary()


def current():
    return _CARRIER[0]


def carrier_for(node):
    """Return the active or registered FolderCarrier for ``node``, or None."""
    if _CARRIER[0] is not None and _CARRIER[0].node is node:
        return _CARRIER[0]
    return _CARRIERS.get(node)


def on_code_changed(node):
    """Reset folder carrier state for the new room tag and re-create/write on the host."""
    st = node.store
    for k, v in (("folder_done", []), ("folder_head_seen", {}), ("folder_join_seen", {}),
                 ("folder_welcome_n", {}), ("folder_head_n", 0), ("folder_written_head", None),
                 ("folder_written_settings", None), ("folder_written_at", None), ("folder_written_key", None),
                 ("folder_snap_head", 0), ("folder_snap_at", None), ("folder_head_at", None),
                 ("folder_file_hi", {}), ("folder_file_after", {}), ("folder_file_made", {}),
                 ("folder_base_pkg", None), ("folder_old_base_pkgs", []), ("folder_rebase_join_at", None)):
        st.put(k, v)
    folder = st.get("folder_carrier") or ""
    if not folder:
        return
    c = carrier_for(node)
    if c is not None:
        c._marker_ok = None
        c._pending = ([], [], {})
        c._inflight_meta = {}
        c._waiting_joins = {}
        c._pending_welcomes = []
        c._need_rebase_join = False
        c.status["state"] = _initial_state(c.folder, c.base)
    if pigeon._role(node) == "host":
        create_room_folder(node, folder)
        if c is not None:
            c.write(force=True)
        else:
            FolderCarrier(node, folder).write(force=True)


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
