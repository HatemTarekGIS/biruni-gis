"""A computer that shares itself: it takes tiles from the room's board while its user is not editing, runs each in a
separate low-priority process (runner.py) with a time limit, and sends back the result signed with its member key.

Sharing is off until the user turns it on, and is remembered per room. The limit says how much of the computer is
given: "light" (one tile at a time, the lowest priority), "half" (half the cores) or "full" (all cores but one).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from . import jobs

LIMITS = ("light", "half", "full")
TILE_TIMEOUT = 300          # seconds one tile may run
POLL = 3.0                  # seconds between two questions to the board when there is nothing to do
SETTINGS = "biruni/team_compute/"


class TileFailed(RuntimeError):
    pass


def python_exe():
    """The engine's own python (Biruni runs inside qgis-bin.exe, which is not a python)."""
    for p in (os.path.join(sys.prefix, "python.exe"), os.path.join(sys.base_prefix, "python.exe"), sys.executable):
        if p and os.path.isfile(p) and os.path.basename(p).lower().startswith("python"):
            return p
    return sys.executable


def slots(limit):
    n = os.cpu_count() or 2
    return {"light": 1, "half": max(1, n // 2), "full": max(1, n - 1)}.get(limit, 1)


def run_local(task, params, data, timeout=TILE_TIMEOUT, limit="light"):
    """Run one tile on this computer, in a separate process. -> result, or TileFailed."""
    params = jobs.validate(task, params)
    jobs.check_tile(task, data)
    folder = tempfile.mkdtemp(prefix="biruni-tile-")
    try:
        with open(os.path.join(folder, "tile.json"), "w", encoding="utf-8") as f:
            json.dump({"task": task, "params": params, "data": data}, f)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p and os.path.isdir(p))
        env["QT_QPA_PLATFORM"] = "offscreen"
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | (subprocess.IDLE_PRIORITY_CLASS if limit == "light"
                                                   else subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runner.py")
        p = subprocess.Popen([python_exe(), runner, folder], cwd=folder, env=env, creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            p.kill()     # our own child
            p.wait(10)
            raise TileFailed("the tile took too long") from None
        try:
            with open(os.path.join(folder, "result.json"), encoding="utf-8") as f:
                out = json.load(f)
        except (OSError, ValueError):
            raise TileFailed("the tool stopped without an answer") from None
        if "error" in out:
            raise TileFailed(out["error"])
        return out["result"]
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ---- talking to the board -----------------------------------------------------------------------------------------
class HostLink:
    """The board on this very computer (the host): no message, the host's own member entry."""

    def __init__(self, hub):
        self.hub = hub

    def call(self, path, obj):
        status, out = jobs.board_of(self.hub).route(path, obj, self.hub.me)
        if status != 200:
            raise jobs.JobError(out.get("error", "refused"))
        return out


class MemberLink:
    """The board on the host, through the room's sealed messages."""

    def __init__(self, node):
        self.node = node

    def call(self, path, obj):
        from ..team import wire

        n = self.node
        if n.client is None or not n.member:
            raise wire.WireError("not connected")
        try:
            return n.client.call(path, dict(obj, token=n.member["token"]), timeout=60)
        except wire.WireError as e:
            if e.status == 400:
                raise jobs.JobError(str(e)) from None
            raise


def link_of(node):
    if node is None:
        return None
    if node.role == "host" and node.hub is not None:
        return HostLink(node.hub)
    if node.role == "member":
        return MemberLink(node)
    return None


def room_id(node):
    return getattr(getattr(node, "keys", None), "tag", "") or ""


def load_share(room):
    from qgis.core import QgsSettings

    s = QgsSettings()
    on = s.value(SETTINGS + "share/" + room, False, type=bool) if room else False
    limit = s.value(SETTINGS + "limit/" + room, "light") if room else "light"
    return bool(on), (limit if limit in LIMITS else "light")


def save_share(room, on, limit):
    from qgis.core import QgsSettings

    s = QgsSettings()
    s.setValue(SETTINGS + "share/" + room, bool(on))
    s.setValue(SETTINGS + "limit/" + room, limit if limit in LIMITS else "light")


# ---- the worker ---------------------------------------------------------------------------------------------------
class Worker:
    """Takes tiles while ``idle`` is true. ``link_fn`` gives the current link (it changes when the host does),
    ``ident`` is this computer's member identity ({"id", "seed"}). ``run`` is run_local, or a stand-in in the tests."""

    def __init__(self, link_fn, ident, limit="light", run=run_local, name=""):
        self.link_fn = link_fn
        self.ident = ident
        self.limit = limit if limit in LIMITS else "light"
        self.run = run
        self.name = name
        self.idle = True               # set by the main thread (no layer being edited)
        self.done = 0
        self.failed = 0
        self.last_error = ""
        self._busy = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._kids = []

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="biruni-team-compute")
        self._thread.start()

    def stop(self, wait=5):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(wait)
        for t in list(self._kids):
            t.join(wait)
        self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            got = False
            if self.idle and self._busy < slots(self.limit):
                link = self.link_fn()
                if link is not None:
                    try:
                        t = link.call("work_get", {"limit": self.limit, "busy": self._busy})
                    except Exception as e:  # noqa: BLE001 - the room comes and goes; try again later
                        self.last_error = str(e)
                        t = {}
                    if t.get("oid"):
                        got = True
                        with self._lock:
                            self._busy += 1
                        th = threading.Thread(target=self._one, args=(link, t), daemon=True)
                        self._kids = [k for k in self._kids if k.is_alive()] + [th]
                        th.start()
            self._stop.wait(0.2 if got else POLL)

    def _one(self, link, t):
        try:
            try:
                result = self.run(t["task"], t["params"], t["data"], limit=self.limit)
                digest = jobs.result_hash(result)
                msg = {"job": t["job"], "oid": t["oid"], "result": result, "hash": digest,
                       "sig": jobs.sign_result(self.ident["seed"], t["job"], t["tid"], digest, self.ident["id"])}
            except (TileFailed, jobs.JobError) as e:
                self.failed += 1
                msg = {"job": t["job"], "oid": t["oid"], "error": str(e)}
            try:
                link.call("work_put", msg)
                if "result" in msg:
                    self.done += 1
            except Exception as e:  # noqa: BLE001 - the owner gives the tile to another one
                self.last_error = str(e)
        finally:
            with self._lock:
                self._busy -= 1


_SHARED = {}    # room id -> Worker of this computer


def sharing(node):
    return _SHARED.get(room_id(node))


def start_sharing(node, limit, idle_fn=None):
    """Turn on sharing this computer in the room of ``node`` (remembered)."""
    from .. import team

    room = room_id(node)
    save_share(room, True, limit)
    w = _SHARED.get(room)
    if w is None:
        def link_fn():
            cur = team.current()
            return link_of(cur) if cur is not None and room_id(cur) == room else None

        w = Worker(link_fn, node.ident, limit, name=node.ident.get("name", ""))
        _SHARED[room] = w
    w.limit = limit
    w.start()
    return w


def stop_sharing(node, forget=True):
    room = room_id(node)
    if forget:
        on, limit = load_share(room)
        save_share(room, False, limit)
    w = _SHARED.pop(room, None)
    if w is not None:
        w.stop()


def on_room(node):
    """A room was opened or joined: start sharing again if the user chose it for this room."""
    if node is None:
        for w in _SHARED.values():
            w.stop(wait=0)
        _SHARED.clear()
        return
    try:
        on, limit = load_share(room_id(node))
        if on:
            start_sharing(node, limit)
            from .ui import _IdleWatch

            _IdleWatch.ensure()
    except Exception:  # noqa: BLE001 - never stop a room from opening
        pass
