"""The computer that asked for a job: it posts the tiles, keeps a lane of its own, checks the workers and stitches.

* Its own lane takes tiles nobody took yet, so a job is never slower than on this computer alone, and finishes
  even when every worker leaves.
* A tile a worker took and did not answer in time is computed here.
* A result must carry the signature of the member who took the tile.
* Checks: every tile of a worker this room has not worked with yet, and a random share of the others, is computed
  a second time - by another worker, or here when none takes it soon. The two hashes must be the same. When they
  are not, this computer computes the tile itself and decides; the worker that was wrong is marked (remembered for
  the room), and every tile it gave in this job is computed again.
"""
import json
import random
import threading
import time

from . import jobs, split
from .worker import run_local

CHECK_SHARE = 0.10
TAKE_TIMEOUT = 600          # seconds a worker may keep a tile
CHECK_WAIT = 30             # seconds a second opinion may take before this computer gives it
POST_BYTES = 3 << 20        # bytes of tiles sent in one message


def pubs_of(node):
    """{member id: public key} of the room, as this computer knows it."""
    if node is None:
        return {}
    roster = node.hub.roster() if node.role == "host" and node.hub is not None else (getattr(node, "roster", None)
                                                                                         or [])
    return {m["id"]: m.get("pub") for m in roster if m.get("id") and m.get("pub")}


def load_trust(room):
    from qgis.core import QgsSettings

    try:
        return json.loads(QgsSettings().value("biruni/team_compute/trust/" + room, "{}") or "{}")
    except ValueError:
        return {}


def save_trust(room, trust):
    from qgis.core import QgsSettings

    QgsSettings().setValue("biruni/team_compute/trust/" + room, json.dumps(trust))


class Owner:
    """Runs one job in a thread. Watch ``tiles_state`` ({tid: (state, who)}), ``log`` and ``done``; the answer is
    in ``result`` (the tiles' results by tid) or ``error``."""

    def __init__(self, link, task, params, tiles, pubs, trust=None, on_trust=None, run=run_local, local_slots=1,
                 check_share=CHECK_SHARE, take_timeout=TAKE_TIMEOUT, check_wait=CHECK_WAIT, seed=None, poll=0.4, me=None):
        self.link = link
        self.me = me                # this computer's member id: the tiles its own lane took
        self.task = task
        self.params = jobs.validate(task, params)
        self.tiles = {t["tid"]: t for t in tiles}
        self.pubs = pubs
        self.trust = trust if trust is not None else {}
        self.on_trust = on_trust or (lambda t: None)
        self.run = run
        self.local_slots = max(1, local_slots)
        self.check_share = check_share
        self.take_timeout = take_timeout
        self.check_wait = check_wait
        self.rng = random.Random(seed)
        self.poll = poll
        self.job = jobs.new_job_id()
        self.tiles_state = {tid: ("waiting", "") for tid in self.tiles}
        self.accepted = {}          # tid -> (result, by)
        self.held = {}              # tid -> {"by", "hash", "result", "since"}: waits for a second opinion
        self.judge = {}             # tid -> [(by, hash)] two answers that differ: this computer decides
        self.local_q = []           # tiles this computer computes itself (not through the board)
        self.new = {}               # worker id -> new to this room when the job started
        self.bad = set(k for k, v in self.trust.items() if v == "bad")
        self.workers = []
        self.by_worker = {}         # worker id -> tiles done
        self.log = []
        self.result = None
        self.error = None
        self.done = threading.Event()
        self._cancel = threading.Event()
        self._busy = 0
        self._local_lock = threading.Lock()
        self._local_done = []       # (tid, result | None, error)
        self._thread = None

    # ---- control -------------------------------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._main, daemon=True, name="biruni-team-job")
        self._thread.start()
        return self

    def cancel(self):
        self._cancel.set()

    def wait(self, timeout=None):
        return self.done.wait(timeout)

    def _say(self, text):
        self.log.append((time.time(), text))

    # ---- the loop ------------------------------------------------------------------------------------------
    def _main(self):
        try:
            self._post()
            while len(self.accepted) < len(self.tiles):
                if self._cancel.is_set():
                    raise RuntimeError("cancelled")
                self._turn()
                self._cancel.wait(self.poll)
            self.result = {tid: r for tid, (r, _by) in self.accepted.items()}
        except Exception as e:  # noqa: BLE001 - shown to the user
            self.error = str(e)
        finally:
            try:
                self.link.call("work_end", {"job": self.job})
            except Exception:  # noqa: BLE001
                pass
            self.on_trust(self.trust)
            self.done.set()

    def _post(self):
        first, batch, size = True, [], 0
        items = [{"tid": t["tid"], "data": t["data"]} for t in self.tiles.values()]
        for it in items + [None]:
            s = jobs.size_of(it["data"]) if it else 0
            if it is None or (batch and size + s > POST_BYTES):
                msg = {"job": self.job, "tiles": batch}
                if first:
                    msg.update(task=self.task, params=self.params)
                self.link.call("work_post", msg)
                first, batch, size = False, [], 0
            if it is not None:
                batch.append(it)
                size += s

    def _turn(self):
        r = self.link.call("work_take", {"job": self.job})
        self.workers = r.get("workers") or []
        now = r.get("now", time.time())
        for res in r.get("results") or []:
            self._from_worker(res)
        with self._local_lock:
            done, self._local_done = self._local_done, []
        for tid, result, err in done:
            self._from_local(tid, result, err)
        offers = r.get("offers") or {}
        for oid, o in offers.items():
            tid = o["tid"]
            if tid in self.accepted or tid in self.local_q:
                continue
            if o["state"] == "taken" and o["by"] and now - o["at"] > self.take_timeout and o["by"] != self.me:
                self._say(f"timeout {tid} {o['by'][:6]}")
                self._queue_local(tid)
            elif o["check"] and o["state"] != "done" and tid in self.held and now - self.held[tid]["since"] > \
                    self.check_wait:
                self._queue_local(tid)
            if o["state"] == "taken" and o["by"] and tid not in self.accepted and not o["check"]:
                self.tiles_state[tid] = ("working", o["by"])
        self._local_lane()

    def _queue_local(self, tid):
        if tid not in self.local_q and tid not in self.accepted:
            self.local_q.append(tid)

    def _local_lane(self):
        while self._busy < self.local_slots:
            tid = None
            while self.local_q:
                cand = self.local_q.pop(0)
                if cand not in self.accepted:
                    tid = cand
                    break
            if tid is None:
                got = self.link.call("work_claim", {"job": self.job})
                tid = got.get("tid")
                if tid is None:
                    return
            self._busy += 1
            self.tiles_state[tid] = ("working", "here")
            threading.Thread(target=self._compute_here, args=(tid,), daemon=True).start()

    def _compute_here(self, tid):
        try:
            res, err = self.run(self.task, self.params, self.tiles[tid]["data"]), ""
        except Exception as e:  # noqa: BLE001
            res, err = None, str(e)
        with self._local_lock:
            self._local_done.append((tid, res, err))
            self._busy -= 1

    # ---- deciding --------------------------------------------------------------------------------------------
    def _accept(self, tid, result, by):
        self.accepted[tid] = (result, by)
        self.held.pop(tid, None)
        self.judge.pop(tid, None)
        self.tiles_state[tid] = ("done", by)
        self.by_worker[by] = self.by_worker.get(by, 0) + 1

    def _agree(self, *who):
        for w in who:
            if w and w != "here" and self.trust.get(w) != "bad":
                self.trust[w] = int(self.trust.get(w) or 0) + 1

    def _mark_bad(self, w):
        if not w or w == "here" or w in self.bad:
            return
        self.bad.add(w)
        self.trust[w] = "bad"
        self._say(f"wrong answer from {w[:6]}")
        for tid, h in list(self.held.items()):
            if h["by"] == w:
                del self.held[tid]
                self._queue_local(tid)
        for tid, (_r, by) in list(self.accepted.items()):     # nothing it gave is kept
            if by == w:
                del self.accepted[tid]
                self.by_worker[w] -= 1
                self._queue_local(tid)
                self.tiles_state[tid] = ("waiting", "")

    def _from_worker(self, res):
        tid, w = res.get("tid"), res.get("by")
        if tid not in self.tiles or tid in self.accepted:
            return
        if w in self.bad:
            self._queue_local(tid)
            return
        if res.get("error") or res.get("result") is None:
            self._say(f"{tid} failed on {w[:6]}: {res.get('error')}")
            self._queue_local(tid)
            return
        digest = jobs.result_hash(res["result"])
        pub = self.pubs.get(w)
        if digest != res.get("hash") or not pub or not jobs.signed_ok(pub, self.job, tid, digest, w, res.get("sig")):
            self._say(f"{tid}: unsigned answer from {w[:6]}")
            self._mark_bad(w)
            self._queue_local(tid)
            return
        if w not in self.new:
            self.new[w] = int(self.trust.get(w) or 0) <= 0
        h = self.held.get(tid)
        if h is not None:
            if h["by"] == w:
                return
            if h["hash"] == digest:
                self._agree(w, h["by"])
                self._accept(tid, res["result"], w)
            else:
                self.judge[tid] = [(h["by"], h["hash"]), (w, digest)]
                self._queue_local(tid)
            return
        if self.new[w] or self.rng.random() < self.check_share:
            self.held[tid] = {"by": w, "hash": digest, "result": res["result"], "since": time.time()}
            self.tiles_state[tid] = ("checking", w)
            self.link.call("work_post", {"job": self.job, "reoffer": [{"tid": tid, "exclude": [w], "check": True}]})
            return
        self._accept(tid, res["result"], w)

    def _from_local(self, tid, result, err):
        if result is None:
            self.error = err or "a tile failed on this computer"
            raise RuntimeError(self.error)
        digest = jobs.result_hash(result)
        others = list(self.judge.get(tid, []))
        h = self.held.get(tid)
        if h is not None and (h["by"], h["hash"]) not in others:
            others.append((h["by"], h["hash"]))
        for w, hsh in others:
            if hsh == digest:
                self._agree(w)
            else:
                self._mark_bad(w)
        if tid not in self.accepted:
            self._accept(tid, result, "here")


# ---- one job from start to finish, for the panel and the tests ------------------------------------------------------
def make_tiles(task, layer=None, raster_path=None, feats=None, crs_wkt="", target=1500, cell=512, nx=None, ny=None):
    spec = jobs.TASKS[task]
    if spec["kind"] == "vector":
        if feats is None:
            feats = [(f.id(), f.geometry()) for f in layer.getFeatures()]
            crs_wkt = crs_wkt or layer.crs().toWkt()
        return None, split.vector_tiles(feats, task, crs_wkt, target=target, nx=nx, ny=ny)
    return split.raster_tiles(raster_path, task, cell=cell)
