r"""Team computing, off screen: a room's idle computers as one processing grid (team_compute/).

The allowlist and the checked parameters; tiles that carry only numbers and geometries; split and stitch give the
same answer as one run on one computer (overlaps and gaps of parcels, with a courtyard across tiles and a long
strip over many; a dissolved buffer; slope, hillshade and aspect of a DEM, pixel for pixel); the separate process
(runner.py) gives the same bytes as a run here, and is stopped at its time limit; through a real room host over
HTTP with two members sharing their computers: the job finishes, new workers are checked, the results are signed;
a lying worker and a worker with a forged signature are caught, marked and their tiles computed again; a worker
that takes tiles and goes silent is replaced by the owner's own lane; a worker cannot read another's job; other
keys and no token are refused.

Run:  engine\bin\python-qgis.bat tests\test_team_compute.py
"""
import base64
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsProcessingContext, QgsProcessingFeedback,  # noqa: E402
                       QgsProcessingProvider, QgsProcessingUtils, QgsProject, QgsRectangle, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], False)
app.initQgis()
from qgis.analysis import QgsNativeAlgorithms  # noqa: E402

reg = QgsApplication.processingRegistry()
if reg.providerById("native") is None:
    reg.addProvider(QgsNativeAlgorithms())

from biruni.algorithms.overlaps_gaps import OverlapsGaps  # noqa: E402


class _P(QgsProcessingProvider):
    def id(self):
        return "biruni"

    def name(self):
        return "Biruni GIS"

    def loadAlgorithms(self):  # noqa: N802
        self.addAlgorithm(OverlapsGaps())


_prov = _P()
reg.addProvider(_prov)

import numpy as np  # noqa: E402
from osgeo import gdal  # noqa: E402

from biruni.secure import crypto  # noqa: E402
from biruni.team import code as codes, wire  # noqa: E402
from biruni.team.hub import Hub  # noqa: E402
from biruni.team.store import Store  # noqa: E402
from biruni.team_compute import compute, jobs, owner, split, worker  # noqa: E402

failures = []
TMP = tempfile.mkdtemp(prefix="biruni-tc-")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def raises(fn, exc=jobs.JobError):
    try:
        fn()
    except exc:
        return True
    except Exception as e:  # noqa: BLE001
        print("   unexpected", type(e).__name__, e)
        return False
    return False


# ---- the allowlist -------------------------------------------------------------------------------------------
check(raises(lambda: jobs.validate("native:shellcommand", {})), "a tool outside the allowlist is refused")
check(raises(lambda: jobs.validate("biruni:overlapsgaps", {})), "an algorithm id is not a task name")
check(jobs.validate("slope", {}) == {"Z_FACTOR": 1.0}, "defaults are filled in")
check(raises(lambda: jobs.validate("slope", {"Z_FACTOR": -1})), "a value out of range is refused")
check(raises(lambda: jobs.validate("slope", {"Z_FACTOR": "1; rm"})), "a text for a number is refused")
check(raises(lambda: jobs.validate("slope", {"Z_FACTOR": True})), "a yes/no for a number is refused")
check(raises(lambda: jobs.validate("slope", {"OUTPUT": "C:/x.tif"})), "a parameter the task has not is refused")
check(raises(lambda: jobs.validate("bufferdissolve", {"SEGMENTS": 2.5})), "a fraction for a whole number is refused")
check(raises(lambda: jobs.validate("bufferdissolve", {"DISTANCE": float("nan")})), "NaN is refused")
check(raises(lambda: jobs.check_tile("bufferdissolve", {"feats": [], "owned": [], "core": [0, 0, 1, 1],
                                                         "path": "C:/Windows"})), "a tile with a path is refused")
check(raises(lambda: jobs.check_tile("bufferdissolve", {"feats": [[1, "zz"]], "owned": [], "core": [0, 0, 1, 1]})),
      "a tile with a broken geometry is refused")
check(raises(lambda: jobs.check_tile("slope", {"w": 4, "h": 4, "gt": [0, 1, 0, 0, 0, -1], "dtype": "object",
                                               "nodata": None, "data": "", "halo": [0, 0, 0, 0]})),
      "a raster tile of an unknown type is refused")
check(raises(lambda: jobs.check_tile("slope", {"w": 4, "h": 4, "gt": [0, 1, 0, 0, 0, -1], "dtype": "float32",
                                               "nodata": None, "data": "AAAA", "halo": [0, 0, 0, 0]})),
      "a raster tile whose pixels do not match its size is refused")

# ---- the data ------------------------------------------------------------------------------------------------
X0, Y0 = 500000.0, 2700000.0


def sq(x, y, s=10.0, grow=0.0, dx=0.0):
    return QgsGeometry.fromRect(QgsRectangle(X0 + x * s - grow + dx, Y0 + y * s - grow, X0 + (x + 1) * s + grow + dx,
                                             Y0 + (y + 1) * s + grow))


parcels = QgsVectorLayer("Polygon?crs=EPSG:32638", "parcels", "memory")
feats = []
for y in range(9):
    for x in range(12):
        if (x, y) == (3, 3) or (6 <= x <= 8 and 4 <= y <= 6):
            continue                                  # a gap, and a courtyard across two tiles
        g = sq(x, y, dx=2.0 if (x, y) == (1, 1) else 0.0, grow=1.0 if (x, y) == (9, 7) else 0.0)
        f = QgsFeature()
        f.setGeometry(g)
        feats.append(f)
strip = QgsFeature()
strip.setGeometry(QgsGeometry.fromRect(QgsRectangle(X0 + 5, Y0 + 15.5, X0 + 115, Y0 + 16.5)))   # over many cells
feats.append(strip)
parcels.dataProvider().addFeatures(feats)
QgsProject.instance().addMapLayer(parcels)
check(parcels.featureCount() == len(feats), f"{len(feats)} parcels")


def run_alg(alg, params):
    ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
    ctx.setProject(QgsProject.instance())
    a = reg.createAlgorithmById(alg)
    res, ok = a.run(params, ctx, fb)
    assert ok, alg
    return res, ctx


# one run on one computer
res, ctx = run_alg("biruni:overlapsgaps", {"INPUT": parcels, "MIN_AREA": 0.0, "MAX_GAP": 0.0,
                                         "OVERLAPS": "TEMPORARY_OUTPUT", "GAPS": "TEMPORARY_OUTPUT"})
ov1 = QgsProcessingUtils.mapLayerFromString(res["OVERLAPS"], ctx)
gp1 = QgsProcessingUtils.mapLayerFromString(res["GAPS"], ctx)
one_ov = sorted([f.geometry().asWkb().data().hex(), int(f["feature_a"]), int(f["feature_b"]), float(f["area"])]
                for f in ov1.getFeatures())
one_ov.sort(key=lambda r: (r[1], r[2]))
one_gaps = [f.geometry() for f in gp1.getFeatures()]
check(len(one_ov) > 10 and len(one_gaps) >= 3 and any(895 < g.area() < 900 for g in one_gaps), f"one computer: {len(one_ov)} overlaps, {len(one_gaps)} gaps")

LOCK = threading.Lock()


def run_here(task, params, data, limit=None):
    """A tile in this process (the tests' stand-in for the separate process; one at a time)."""
    with LOCK:
        folder = tempfile.mkdtemp(dir=TMP)
        try:
            return compute.run_tile(task, params, data, folder)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def same_gaps(a_wkbs, b_geoms):
    if len(a_wkbs) != len(b_geoms):
        return False
    left = list(b_geoms)
    for h, area in a_wkbs:
        g = QgsGeometry()
        g.fromWkb(bytes.fromhex(h))
        m = next((o for o in left if g.symDifference(o).area() < 1e-6 and abs(o.area() - area) < 1e-6), None)
        if m is None:
            return False
        left.remove(m)
    return True


def tiled(task, params, tiles, run=run_here):
    return {t["tid"]: run(task, params, t["data"]) for t in tiles}


_, vt = owner.make_tiles("overlapsgaps", parcels, nx=3, ny=3)
check(len(vt) == 9, f"split into {len(vt)} tiles")
owned = [i for t in vt for i in t["data"]["owned"]]
check(sorted(owned) == sorted(f.id() for f in parcels.getFeatures()), "every feature belongs to exactly one tile")
check(all(set(t["data"]) <= {"crs", "feats", "owned", "core"} for t in vt), "a tile carries only data, no paths")
st = split.stitch_vector("overlapsgaps", {}, tiled("overlapsgaps", {}, vt))
check(st["overlaps"] == one_ov, "tiled overlaps = one computer's, byte for byte")
check(same_gaps(st["gaps"], one_gaps), "tiled gaps = one computer's (the courtyard across tiles included)")
_, vt2 = owner.make_tiles("overlapsgaps", parcels, nx=5, ny=2)
st2 = split.stitch_vector("overlapsgaps", {"MIN_AREA": 0.0}, tiled("overlapsgaps", {}, vt2))
check(st2["overlaps"] == one_ov and same_gaps(st2["gaps"], one_gaps), "another grid gives the same answer")
st3 = split.stitch_vector("overlapsgaps", {"MAX_GAP": 500.0}, tiled("overlapsgaps", {"MAX_GAP": 500.0}, vt))
check(sorted(round(a, 6) for _w, a in st3["gaps"]) == sorted(round(g.area(), 6) for g in one_gaps if g.area() <= 500)
      and len(st3["gaps"]) < len(one_gaps), "the largest-gap limit applies to the whole (the courtyard is left out)")

bp = {"DISTANCE": 3.0, "SEGMENTS": 8}
res, ctx = run_alg("native:buffer", {"INPUT": parcels, "DISTANCE": 3.0, "SEGMENTS": 8, "END_CAP_STYLE": 0,
                                     "JOIN_STYLE": 0, "MITER_LIMIT": 2.0, "DISSOLVE": True, "OUTPUT": "TEMPORARY_OUTPUT"})
one_buf = next(QgsProcessingUtils.mapLayerFromString(res["OUTPUT"], ctx).getFeatures()).geometry()
_, bt = owner.make_tiles("bufferdissolve", parcels, nx=3, ny=2)
check(all(set(t["data"]["owned"]) == {f[0] for f in t["data"]["feats"]} for t in bt),
      "a buffer tile carries its own features only")
sb = split.stitch_vector("bufferdissolve", bp, tiled("bufferdissolve", bp, bt))
g = QgsGeometry()
g.fromWkb(bytes.fromhex(sb["geom"]))
check(abs(g.area() - one_buf.area()) < 1e-6 and g.symDifference(one_buf).area() < 1e-6,
      f"tiled dissolved buffer = one computer's ({g.area():.1f} m²)")

# a DEM
W, H = 300, 220
yy, xx = np.mgrid[0:H, 0:W]
dem = (50 + 20 * np.sin(xx / 17.0) * np.cos(yy / 23.0) + 0.05 * xx).astype("float32")
dem[100:110, 40:55] = -9999
dem_path = os.path.join(TMP, "dem.tif")
ds = gdal.GetDriverByName("GTiff").Create(dem_path, W, H, 1, gdal.GDT_Float32)
ds.SetGeoTransform([X0, 5, 0, Y0 + H * 5, 0, -5])
from osgeo import osr  # noqa: E402

srs = osr.SpatialReference()
srs.ImportFromEPSG(32638)
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).SetNoDataValue(-9999)
ds.GetRasterBand(1).WriteArray(dem)
ds = None


def read(path):
    d = gdal.Open(path)
    a = d.GetRasterBand(1).ReadAsArray()
    gt = d.GetGeoTransform()
    d = None
    return a, gt


for task, extra in (("slope", {}), ("hillshade", {"AZIMUTH": 300.0, "V_ANGLE": 35.0}), ("aspect", {})):
    single = os.path.join(TMP, task + "_one.tif")
    p = dict(jobs.validate(task, extra), INPUT=dem_path, OUTPUT=single)
    run_alg(jobs.TASKS[task]["alg"], p)
    meta, rt = owner.make_tiles(task, raster_path=dem_path, cell=64)
    out = split.stitch_raster(meta, rt, tiled(task, extra, rt), os.path.join(TMP, task + "_tiled.tif"))
    a, gta = read(single)
    b, gtb = read(out)
    check(a.shape == b.shape and np.array_equal(a.astype("float32"), b, equal_nan=True) and gta == gtb,
          f"tiled {task} = one computer's, pixel for pixel ({len(rt)} tiles)")

# ---- the separate process --------------------------------------------------------------------------------------
t0 = time.time()
r_here = run_here("overlapsgaps", {}, vt[4]["data"])
r_sub = worker.run_local("overlapsgaps", {}, vt[4]["data"], timeout=120)
check(jobs.result_hash(r_sub) == jobs.result_hash(r_here), f"the separate process gives the same bytes "
                                                           f"({time.time() - t0:.1f} s)")
rr = worker.run_local("slope", {}, rt[3]["data"], timeout=120)
check(jobs.result_hash(rr) == jobs.result_hash(run_here("slope", {}, rt[3]["data"])),
      "the separate process: a raster tile too")
check(raises(lambda: worker.run_local("slope", {}, rt[3]["data"], timeout=0.05), worker.TileFailed),
      "a tile past its time limit is stopped")
check(raises(lambda: worker.run_local("native:shellcommand", {}, {})), "the worker refuses a tool outside the list")
check(not [d for d in os.listdir(tempfile.gettempdir()) if d.startswith("biruni-tile-")
           and os.path.getmtime(os.path.join(tempfile.gettempdir(), d)) > t0], "the tiles' private folders are removed")

# ---- a real room host -------------------------------------------------------------------------------------------
keys = codes.RoomKeys(codes.new_code())
store = Store(os.path.join(TMP, "room", "room.sqlite"))


def ident(name):
    seed = secrets.token_hex(32)
    return {"id": secrets.token_hex(16), "seed": seed, "name": name,
            "pub": crypto.public_key(bytes.fromhex(seed)).hex(), "token": secrets.token_hex(16)}


host_id = ident("host")
me = {"id": host_id["id"], "name": "host", "pub": host_id["pub"], "token": host_id["token"], "rank": 0}
members = {n: ident(n) for n in ("amal", "badr", "liar", "forger", "silent")}
for i, m in enumerate(members.values()):
    store.save_member({"id": m["id"], "name": m["name"], "pub": m["pub"], "rank": i + 1, "rights": {},
                       "token": m["token"], "joined": time.time(), "seen": time.time(), "state": "in"})
hub = Hub(store, keys, {}, me)
port = hub.start(port=47600, bind="127.0.0.1")


class Link:
    def __init__(self, m, key=None):
        self.c = wire.Client(wire.Sealer(key or keys.key), f"http://127.0.0.1:{port}")
        self.token = m["token"] if m else None

    def call(self, path, obj):
        try:
            return self.c.call(path, dict(obj, token=self.token), timeout=30)
        except wire.WireError as e:
            if e.status == 400:
                raise jobs.JobError(str(e)) from None
            raise


try:
    Link(members["amal"], key=codes.RoomKeys(codes.new_code()).key).call("work_info", {})
    check(False, "another room's key is refused")
except wire.WireError as e:
    check(e.status == 403, "another room's key is refused")
try:
    Link(None).call("work_info", {})
    check(False, "no token is refused")
except wire.WireError as e:
    check(e.status == 401, "a computer that is not a member is refused")
check(Link(members["amal"]).call("work_get", {}) == {}, "nothing to do: an empty answer")


def lying(task, params, data, limit=None):
    r = run_here(task, params, data)
    if r.get("overlaps"):
        r["overlaps"] = r["overlaps"][:-1]       # leaves one overlap out
    else:
        r["piece"] = ""
    return r


def start(name, run=run_here, sign_with=None):
    m = members[name]
    w = worker.Worker(lambda m=m: Link(m), {"id": m["id"], "seed": sign_with or m["seed"]}, "full", run=run, name=name)
    w.start()
    return w


def job(tiles, trust, **kw):
    pubs = {m["id"]: m["pub"] for m in members.values()}
    pubs[me["id"]] = me["pub"]
    o = owner.Owner(worker.HostLink(hub), "overlapsgaps", {}, tiles, pubs, trust=trust, run=run_here, seed=7,
                    me=me["id"], **kw)
    o.start()
    ok = o.wait(240)
    return o, ok


def answer(o):
    return split.stitch_vector("overlapsgaps", {}, o.result or {})


w1, w2 = start("amal"), start("badr")
time.sleep(0.3)
info = worker.HostLink(hub).call("work_info", {})
check({x["name"] for x in info["workers"]} >= {"amal", "badr"}, "the host sees who shares their computer")
trust = {}
_, vt6 = owner.make_tiles("overlapsgaps", parcels, nx=4, ny=3)
o, ok = job(vt6, trust, check_wait=60)
check(ok and o.error is None, f"a job through the room finishes ({o.error or 'ok'})")
st = answer(o)
check(st["overlaps"] == one_ov and same_gaps(st["gaps"], one_gaps), "the room's answer = one computer's")
done_by = {k: v for k, v in o.by_worker.items() if v}
check(done_by.get(members["amal"]["id"], 0) + done_by.get(members["badr"]["id"], 0) > 0,
      f"the members did tiles: {[(members_n, done_by.get(m['id'], 0)) for members_n, m in members.items() if m['id'] in done_by]}"
      f" here={done_by.get('here', 0)}")
check(all(isinstance(trust.get(members[n]["id"]), int) and trust[members[n]["id"]] > 0
          for n in ("amal", "badr") if done_by.get(members[n]["id"])),
      "new workers were checked and agreed: they count as trusted now")
check(not hub._work_board.jobs, "the board forgets a finished job")
w1.stop()
w2.stop()

# a lying worker among honest ones
trust2 = {}
wl, wa = start("liar", run=lying), start("amal")
o, ok = job(vt6, trust2, check_wait=60)
st = answer(o)
check(ok and o.error is None and st["overlaps"] == one_ov and same_gaps(st["gaps"], one_gaps),
      "with a lying worker the answer is still right")
check(trust2.get(members["liar"]["id"]) == "bad", "the lying worker is caught and marked")
check(trust2.get(members["amal"]["id"]) != "bad", "the honest one is not")
check(o.by_worker.get(members["liar"]["id"], 0) == 0, "nothing the lying worker gave is kept")
wl.stop()
# the mark is remembered: next time it is not even asked
wl2 = start("liar", run=lying)
o, ok = job(vt6, trust2, check_wait=60)
check(ok and o.by_worker.get(members["liar"]["id"], 0) == 0 and answer(o)["overlaps"] == one_ov,
      "a marked worker's answers are not used in the next job")
wl2.stop()
wa.stop()

# a forged signature (signed with another key)
trust3 = {}
wf = start("forger", sign_with=secrets.token_hex(32))
o, ok = job(vt6[:4], trust3, check_wait=60)
check(ok and trust3.get(members["forger"]["id"]) == "bad" and o.by_worker.get(members["forger"]["id"], 0) == 0,
      "an answer that is not signed by the member who took the tile is refused")
wf.stop()

# a worker that takes tiles and goes silent
stolen = []


class Silent(threading.Thread):
    def run(self):
        link = Link(members["silent"])
        end = time.time() + 20
        while time.time() < end and len(stolen) < 3:
            t = link.call("work_get", {})
            if t.get("oid"):
                stolen.append(t["tid"])
            time.sleep(0.05)


sil = Silent(daemon=True)
sil.start()
trust4 = {}
o = owner.Owner(worker.HostLink(hub), "overlapsgaps", {}, vt6, {m["id"]: m["pub"] for m in members.values()},
                trust=trust4, run=lambda *a, **k: (time.sleep(0.3), run_here(*a, **k))[1], take_timeout=2,
                me=me["id"], seed=1)
o.start()
ok = o.wait(240)
sil.join(5)
check(ok and o.error is None and len(stolen) >= 1 and answer(o)["overlaps"] == one_ov,
      f"tiles a silent worker took ({len(stolen)}) are computed by the owner after the time limit")
check(any("timeout" in t for _, t in o.log), "the owner says which tiles timed out")

# nobody shares: the owner's own lane does all
o, ok = job(vt6[:3], {})
check(ok and o.by_worker.get("here", 0) == 3, "with no workers the owner computes every tile itself")

# a member cannot read another's job
o = owner.Owner(worker.HostLink(hub), "overlapsgaps", {}, vt6[:2], {}, run=lambda *a, **k: time.sleep(5) or {},
                me=me["id"])
o._post()
check(raises(lambda: Link(members["amal"]).call("work_take", {"job": o.job})), "a member cannot read another's job")
check(raises(lambda: Link(members["amal"]).call("work_post", {"job": "x", "task": "nope", "tiles": []})),
      "the board refuses a job with a tool outside the list")
check(raises(lambda: Link(members["amal"]).call("work_put", {"job": o.job, "oid": "v0_0.1", "result": {}})),
      "a result for a tile the member did not take is refused")
worker.HostLink(hub).call("work_end", {"job": o.job})

# ---- the windows (no room: this computer alone) ------------------------------------------------------------------
from biruni.team_compute import ui  # noqa: E402


class _Bar:
    def pushMessage(self, *a):  # noqa: N802
        self.last = a


class _Iface:
    bar = _Bar()

    def mainWindow(self):  # noqa: N802
        return None

    def messageBar(self):  # noqa: N802
        return self.bar


class _Plugin:
    iface = _Iface()


d = ui.RunDialog(_Plugin())
check(d.task.count() == len(jobs.TASKS), "the window lists exactly the allowed tools")
d.task.setCurrentIndex(d.task.findData("overlapsgaps"))
d.source.setCurrentIndex(d.source.findData(parcels.id()))
before = set(QgsProject.instance().mapLayers())
d._start()
end = time.time() + 240
while time.time() < end and not (d.job and d.job.done.is_set() and not d.timer.isActive()):
    app.processEvents()
    time.sleep(0.05)
added = [QgsProject.instance().mapLayer(i) for i in set(QgsProject.instance().mapLayers()) - before]
ov = next((l for l in added if l.fields().indexOf("feature_b") >= 0), None)
check(d.job.error is None and ov is not None and ov.featureCount() == len(one_ov),
      f"the window runs a job on this computer alone and adds the overlaps ({ov.featureCount() if ov else 0})")
check(any(l.fields().indexOf("gap_id") >= 0 and l.featureCount() == len(one_gaps) for l in added),
      "and the gaps")
d.reject()


class _Keys:
    tag = "testroom"


class _Node:
    keys = _Keys()
    ident = {"id": "x" * 32, "seed": "11" * 32, "name": "me"}
    role = None


nd = _Node()
w = worker.start_sharing(nd, "half")
check(worker.sharing(nd) is w and w.running and worker.load_share("testroom") == (True, "half"),
      "sharing is turned on and remembered for the room")
ui._IdleWatch.ensure()
parcels.startEditing()
parcels.changeGeometry(next(parcels.getFeatures()).id(), sq(0, 0, grow=0.1))
ui._IdleWatch.tick()
check(w.idle is False, "while the user edits, the computer takes no tiles")
parcels.rollBack()
ui._IdleWatch.tick()
check(w.idle is True, "and takes them again after")
worker.stop_sharing(nd)
check(worker.sharing(nd) is None and not w.running and worker.load_share("testroom")[0] is False,
      "sharing is turned off and that is remembered")
worker.save_share("testroom", True, "light")
worker.on_room(nd)
check(worker.sharing(nd) is not None, "opening the room again shares the computer again (the user chose so)")
worker.on_room(None)
check(worker.sharing(nd) is None, "closing the room stops sharing")

hub.stop()
store.close()
shutil.rmtree(TMP, ignore_errors=True)
print("\nALL PASS" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
