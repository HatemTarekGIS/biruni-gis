"""The tools a worker may run, their parameters, and the board of tiles on the room's host.

TASKS is the whole allowlist: a worker refuses any other tool, and every parameter is checked (type and range)
on the board when a job is posted and again on the worker before it runs. The data of a tile is numbers and
geometries (jobs never carry a path, a file name or code).

The board lives on the host (one per hub) and is reached with the room's sealed requests (team/hub.py):

  work_post  the owner of a job sends its tiles (in several calls when they are many), or offers tiles again
  work_get   a worker that shares its computer asks for a tile (answers {} when there is none)
  work_put   the worker's result for a tile, with its signature
  work_take  the owner collects the results that came in, and who has which tile
  work_claim the owner's own lane takes a tile nobody took yet
  work_info  who shares their computer now
  work_end   the owner is done: the board forgets the job
"""
import hashlib
import math
import secrets
import threading
import time
from collections import OrderedDict

from ..secure import crypto

# ---- the allowlist ---------------------------------------------------------------------------------------------
# kind: vector | raster. params: name -> (type, minimum, maximum, default). halo: how far a tile reaches past its
# own part (vector: the neighbours of its features; raster: pixels).
TASKS = OrderedDict([
    ("overlapsgaps", {"kind": "vector", "alg": "biruni:overlapsgaps", "geom": "polygon",
                      "name": "Overlaps and gaps of polygons",
                      "params": {"MIN_AREA": (float, 0.0, 1e12, 0.0), "MAX_GAP": (float, 0.0, 1e15, 0.0)}}),
    ("bufferdissolve", {"kind": "vector", "alg": "native:buffer", "geom": "any",
                        "name": "Buffer, dissolved",
                        "params": {"DISTANCE": (float, 1e-9, 1e7, 10.0), "SEGMENTS": (int, 1, 100, 5),
                                   "END_CAP_STYLE": (int, 0, 2, 0), "JOIN_STYLE": (int, 0, 2, 0),
                                   "MITER_LIMIT": (float, 1.0, 100.0, 2.0)}}),
    ("slope", {"kind": "raster", "alg": "native:slope", "name": "Slope of a DEM", "halo": 1,
               "params": {"Z_FACTOR": (float, 1e-6, 1e6, 1.0)}}),
    ("aspect", {"kind": "raster", "alg": "native:aspect", "name": "Aspect of a DEM", "halo": 1,
                "params": {"Z_FACTOR": (float, 1e-6, 1e6, 1.0)}}),
    ("hillshade", {"kind": "raster", "alg": "native:hillshade", "name": "Hillshade of a DEM", "halo": 1,
                   "params": {"Z_FACTOR": (float, 1e-6, 1e6, 1.0), "AZIMUTH": (float, 0.0, 360.0, 315.0),
                              "V_ANGLE": (float, 0.0, 90.0, 40.0)}}),
    ("ruggedness", {"kind": "raster", "alg": "native:ruggednessindex", "name": "Ruggedness of a DEM", "halo": 1,
                    "params": {"Z_FACTOR": (float, 1e-6, 1e6, 1.0)}}),
])

DTYPES = ("uint8", "int16", "uint16", "int32", "uint32", "float32", "float64")
MAX_TILE = 2 << 20          # bytes of a tile's data (a sealed message of the room is at most 4 MiB)
MAX_FEATS = 20000           # features in one tile
MAX_PIXELS = 1024 * 1024    # pixels in one raster tile (halo included)
MAX_WKT = 20000
MAX_BOARD = 512 << 20       # bytes of tiles the board keeps for all jobs
JOB_TTL = 6 * 3600          # seconds a job stays on the board without its owner
SEEN = 20                   # seconds a worker counts as present after its last request


class JobError(ValueError):
    """A job, a parameter or a tile that is not allowed."""


def validate(task, params):
    """The checked parameters of an allowed task (defaults filled in), or JobError."""
    spec = TASKS.get(task)
    if spec is None:
        raise JobError(f"not an allowed tool: {task}")
    if not isinstance(params, dict):
        raise JobError("bad parameters")
    extra = set(params) - set(spec["params"])
    if extra:
        raise JobError("unknown parameter: " + ", ".join(sorted(map(str, extra))))
    out = {}
    for name, (typ, lo, hi, default) in spec["params"].items():
        v = params.get(name, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise JobError(f"{name} must be a number")
        if typ is int:
            if float(v) != int(v):
                raise JobError(f"{name} must be a whole number")
            v = int(v)
        else:
            v = float(v)
        if not math.isfinite(v) or v < lo or v > hi:
            raise JobError(f"{name} must be between {lo} and {hi}")
        out[name] = v
    return out


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def check_tile(task, data):
    """JobError unless the data of a tile has the shape its task needs (and no more)."""
    spec = TASKS.get(task)
    if spec is None or not isinstance(data, dict):
        raise JobError("bad tile")
    if not isinstance(data.get("crs", ""), str) or len(data.get("crs", "")) > MAX_WKT:
        raise JobError("bad tile: crs")
    if spec["kind"] == "vector":
        if set(data) - {"crs", "feats", "owned", "core"}:
            raise JobError("bad tile: unknown data")
        feats, owned, core = data.get("feats"), data.get("owned"), data.get("core")
        if not isinstance(feats, list) or len(feats) > MAX_FEATS:
            raise JobError("bad tile: features")
        for f in feats:
            if (not isinstance(f, list) or len(f) != 2 or not isinstance(f[0], int) or isinstance(f[0], bool)
                    or not isinstance(f[1], str)):
                raise JobError("bad tile: feature")
            try:
                bytes.fromhex(f[1])
            except ValueError:
                raise JobError("bad tile: geometry") from None
        if not isinstance(owned, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in owned):
            raise JobError("bad tile: owned")
        if not isinstance(core, list) or len(core) != 4 or not all(_num(v) for v in core):
            raise JobError("bad tile: core")
    else:
        if set(data) - {"crs", "w", "h", "gt", "dtype", "nodata", "data", "halo"}:
            raise JobError("bad tile: unknown data")
        w, h, gt = data.get("w"), data.get("h"), data.get("gt")
        if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in (w, h)) or w * h > MAX_PIXELS:
            raise JobError("bad tile: size")
        if not isinstance(gt, list) or len(gt) != 6 or not all(_num(v) for v in gt) or gt[1] == 0 or gt[5] == 0:
            raise JobError("bad tile: geotransform")
        if data.get("dtype") not in DTYPES:
            raise JobError("bad tile: data type")
        if data.get("nodata") is not None and not _num(data["nodata"]):
            raise JobError("bad tile: nodata")
        halo = data.get("halo")
        if (not isinstance(halo, list) or len(halo) != 4
                or not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 8 for v in halo)
                or halo[0] + halo[2] >= w or halo[1] + halo[3] >= h):
            raise JobError("bad tile: halo")
        if not isinstance(data.get("data"), str):
            raise JobError("bad tile: pixels")
        need = w * h * _itemsize(data["dtype"])
        if len(data["data"]) != 4 * ((need + 2) // 3):
            raise JobError("bad tile: pixels")
    return True


def _itemsize(dtype):
    return {"uint8": 1, "int16": 2, "uint16": 2, "int32": 4, "uint32": 4, "float32": 4, "float64": 8}[dtype]


def size_of(data):
    return len(crypto.canonical(data))


# ---- results: one hash, one signature ---------------------------------------------------------------------------
def result_hash(result):
    """The fingerprint two workers' answers are compared by: the same tile gives the same bytes on every computer
    with the same engine."""
    return hashlib.sha256(crypto.canonical(result)).hexdigest()


def sign_body(job, tid, digest, by):
    return crypto.canonical({"job": job, "tid": tid, "hash": digest, "by": by, "what": "biruni-team-compute-1"})


def sign_result(seed_hex, job, tid, digest, by):
    return crypto.sign(bytes.fromhex(seed_hex), sign_body(job, tid, digest, by)).hex()


def signed_ok(pub_hex, job, tid, digest, by, sig_hex):
    try:
        return crypto.verify(bytes.fromhex(pub_hex), bytes.fromhex(sig_hex or "00"), sign_body(job, tid, digest, by))
    except (ValueError, TypeError):
        return False


# ---- the board --------------------------------------------------------------------------------------------------
class Board:
    """The tiles of the room's jobs, kept on the host. Thread-safe: the hub answers in its server threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.jobs = {}              # job id -> {"owner", "task", "params", "tiles": {tid: data}, "offers", "results"}
        self.workers = {}           # member id -> {"name", "limit", "seen", "busy"}
        self.bytes = 0

    # every request comes here with the member the room's token named
    def route(self, path, msg, member):
        if not isinstance(msg, dict):
            return 400, {"error": "bad request"}
        try:
            with self._lock:
                self._expire()
                fn = {"work_post": self._post, "work_get": self._get, "work_put": self._put,
                      "work_take": self._take, "work_claim": self._claim, "work_info": self._info,
                      "work_end": self._end}.get(path)
                if fn is None:
                    return 404, {"error": "unknown request"}
                return 200, fn(msg, member)
        except JobError as e:
            return 400, {"error": str(e)}

    def _job(self, msg, member, own=True):
        job = self.jobs.get(str(msg.get("job", "")))
        if job is None:
            raise JobError("no such job")
        if own and job["owner"] != member["id"]:
            raise JobError("not your job")
        return job

    def _expire(self):
        now = time.time()
        for jid in [j for j, v in self.jobs.items() if now - v["touched"] > JOB_TTL]:
            self._drop(jid)
        for mid in [m for m, v in self.workers.items() if now - v["seen"] > 10 * SEEN]:
            del self.workers[mid]

    def _drop(self, jid):
        job = self.jobs.pop(jid, None)
        if job is not None:
            self.bytes -= job["bytes"]

    def _post(self, msg, member):
        jid = str(msg.get("job", ""))[:40]
        if not jid:
            raise JobError("no job id")
        job = self.jobs.get(jid)
        if job is None:
            task = msg.get("task")
            params = validate(task, msg.get("params") or {})
            job = {"owner": member["id"], "task": task, "params": params, "tiles": {}, "offers": OrderedDict(),
                   "results": [], "bytes": 0, "touched": time.time(), "n": 0}
            self.jobs[jid] = job
        elif job["owner"] != member["id"]:
            raise JobError("not your job")
        job["touched"] = time.time()
        for t in msg.get("tiles") or []:
            tid, data = str(t.get("tid", ""))[:20], t.get("data")
            if not tid or tid in job["tiles"]:
                raise JobError("bad tile id")
            check_tile(job["task"], data)
            size = size_of(data)
            if size > MAX_TILE + 65536:
                raise JobError("tile too large")
            if self.bytes + size > MAX_BOARD:
                raise JobError("the board is full")
            job["tiles"][tid] = data
            job["bytes"] += size
            self.bytes += size
            self._offer(job, tid, [], False)
        for r in msg.get("reoffer") or []:
            tid = str(r.get("tid", ""))
            if tid not in job["tiles"]:
                raise JobError("no such tile")
            for oid, o in list(job["offers"].items()):   # an earlier offer of it that nobody answered is withdrawn
                if o["tid"] == tid and o["state"] != "done" and not o["check"] and not r.get("check"):
                    del job["offers"][oid]
            self._offer(job, tid, [str(x) for x in (r.get("exclude") or [])][:50], bool(r.get("check")))
        return {"tiles": len(job["tiles"]), "offers": sum(1 for o in job["offers"].values() if o["state"] == "open")}

    @staticmethod
    def _offer(job, tid, exclude, check):
        job["n"] += 1
        oid = f"{tid}.{job['n']}"
        job["offers"][oid] = {"tid": tid, "exclude": exclude, "check": check, "state": "open", "by": None, "at": 0.0}
        return oid

    def _get(self, msg, member):
        mid = member["id"]
        w = self.workers.setdefault(mid, {"name": member.get("name", ""), "busy": 0})
        w["seen"] = time.time()
        w["limit"] = str(msg.get("limit", ""))[:10]
        w["busy"] = int(msg.get("busy") or 0) if isinstance(msg.get("busy"), int) else 0
        for jid, job in self.jobs.items():
            if job["owner"] == mid:      # the owner computes its own job in its own lane, not as a worker
                continue
            for oid, o in job["offers"].items():
                if o["state"] == "open" and mid not in o["exclude"]:
                    o.update(state="taken", by=mid, at=time.time())
                    return {"job": jid, "oid": oid, "tid": o["tid"], "task": job["task"], "params": job["params"],
                            "data": job["tiles"][o["tid"]]}
        return {}

    def _claim(self, msg, member):
        job = self._job(msg, member)
        want = msg.get("tid")
        for oid, o in job["offers"].items():
            if o["state"] == "open" and (want is None or o["tid"] == want) and not (want is None and o["check"]):
                o.update(state="taken", by=member["id"], at=time.time())
                return {"oid": oid, "tid": o["tid"]}
        return {}

    def _put(self, msg, member):
        job = self._job(msg, member, own=False)
        o = job["offers"].get(str(msg.get("oid", "")))
        if o is None or o["by"] != member["id"] or o["state"] != "taken":
            raise JobError("not your tile")
        o["state"] = "done"
        res = msg.get("result")
        if res is not None and size_of(res) > 2 * MAX_TILE + 65536:
            raise JobError("result too large")
        job["results"].append({"oid": msg.get("oid"), "tid": o["tid"], "by": member["id"],
                               "name": member.get("name", ""), "check": o["check"], "result": res,
                               "error": str(msg.get("error", ""))[:300] if msg.get("error") else "",
                               "hash": str(msg.get("hash", ""))[:64], "sig": str(msg.get("sig", ""))[:200]})
        return {"ok": True}

    def _take(self, msg, member):
        job = self._job(msg, member)
        job["touched"] = time.time()
        out, job["results"] = job["results"], []
        offers = {oid: {"tid": o["tid"], "state": o["state"], "by": o["by"], "at": o["at"], "check": o["check"]}
                  for oid, o in job["offers"].items()}
        return {"results": out, "offers": offers, "workers": self._workers(), "now": time.time()}

    def _workers(self):
        now = time.time()
        return [{"id": k, "name": v["name"], "limit": v.get("limit", ""), "busy": v.get("busy", 0)}
                for k, v in self.workers.items() if now - v["seen"] < SEEN]

    def _info(self, msg, member):
        return {"workers": self._workers()}

    def _end(self, msg, member):
        self._job(msg, member)
        self._drop(str(msg.get("job")))
        return {"ok": True}


def board_of(hub):
    """The board of one hub (made the first time it is asked for)."""
    b = getattr(hub, "_work_board", None)
    if b is None:
        b = Board()
        hub._work_board = b
    return b


def new_job_id():
    return secrets.token_hex(8)
