r"""A team room under load: one host and N members (default 100) working at once for a minute - each member edits
its own parcel every few seconds, sends where its pointer is every second and keeps a pull open. Measured: the
time a change takes to reach another member, the host's write time, how late the host's own window gets (the
main thread must stay smooth), and what the host sends. The room must keep up with no change lost.

Run:  engine\bin\python-qgis.bat tests\test_team_load.py [members] [seconds]
"""
import os
import random
import secrets
import statistics
import sys
import tempfile
import threading
import time
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
N = int(sys.argv[1]) if len(sys.argv) > 1 else 100
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 60
RTT = float(sys.argv[3]) / 1000 if len(sys.argv) > 3 else 0.0   # a slow connection: this many ms there and back

from qgis.core import (QgsApplication, QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsProject,  # noqa: E402
                       QgsVectorFileWriter, QgsVectorLayer)

os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()
app = QgsApplication([], False)
app.initQgis()

from biruni.secure import crypto  # noqa: E402
from biruni.team import session, wire  # noqa: E402
from biruni.team.features import stored_state  # noqa: E402
from biruni.team.hub import sign_body  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


root = tempfile.mkdtemp()
gpkg = os.path.join(root, "parcels.gpkg")
mem = QgsVectorLayer("Polygon?crs=EPSG:32638&field=owner:string&field=n:integer", "Parcels", "memory")
feats = []
for i in range(2000):
    x, y = (i % 50) * 20, (i // 50) * 20
    f = QgsFeature(mem.fields())
    f.setAttributes([f"start {i}", 0])
    f.setGeometry(QgsGeometry.fromWkt(f"POLYGON(({x} {y},{x + 15} {y},{x + 15} {y + 15},{x} {y + 15},{x} {y}))"))
    feats.append(f)
mem.dataProvider().addFeatures(feats)
o = QgsVectorFileWriter.SaveVectorOptions()
o.driverName = "GPKG"
o.layerName = "parcels"
QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, QgsCoordinateTransformContext(), o)
lyr = QgsVectorLayer(gpkg + "|layername=parcels", "Parcels", "ogr")
QgsProject.instance().addMapLayer(lyr)
QgsProject.instance().write(os.path.join(root, "map.qgz"))
host = session.open_room([lyr], "Load", approve=False)
code = session.room_code(host)
keys = host.keys
address = f"http://127.0.0.1:{host.hub.port}"
states = {fid: stored_state(lyr, fid) for fid in range(1, N + 2)}

stop = threading.Event()
sent_at = {}          # uid -> time sent
seen_lat = []         # seconds from send to seen by another member
push_rt = []
errors = []
lock = threading.Lock()


class Member:
    def __init__(self, i):
        self.i = i
        self.seed = secrets.token_hex(32)
        self.id = secrets.token_hex(16)
        self.pub = crypto.public_key(bytes.fromhex(self.seed)).hex()
        self.client = wire.Client(wire.Sealer(keys.key), address)
        self.fid = i + 1
        self.ver = 0
        self.since = 0
        self.state = dict(states[self.fid])
        self.edits = 0
        self.rv = 0
        self.pts = 0

    def join(self):
        r = wire.say_hello(self.client, {"id": self.id, "name": f"m{self.i}", "pub": self.pub}, self.seed, keys.tag,
                           timeout=60)
        assert r["status"] == "ok", r
        self.token = r["member"]["token"]
        self.since = r["room"]["head"]

    def edit(self):
        base = self.state
        new = {"g": base["g"], "a": dict(base["a"], n=self.edits + 1, owner=f"m{self.i} edit {self.edits + 1}")}
        op = {"uid": uuid.uuid4().hex, "layer": "L1", "fid": self.fid, "kind": "put", "base_ver": self.ver,
              "base": base, "state": new, "author": self.id, "name": f"m{self.i}", "ts": time.time()}
        op["sig"] = crypto.sign(bytes.fromhex(self.seed), sign_body(op)).hex()
        t0 = time.time()
        with lock:
            sent_at[op["uid"]] = t0
        r = self.client.call("push", {"token": self.token, "ops": [op]}, timeout=60)
        push_rt.append(time.time() - t0)
        res = r["results"][0]
        if res["status"] not in ("ok", "merged"):
            errors.append(res)
        self.state = new
        self.edits += 1

    def run_edits(self):
        time.sleep(random.random() * 3)
        while not stop.is_set():
            try:
                self.edit()
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
            stop.wait(2.5 + random.random() * 1.5)

    def run_pull(self):
        while not stop.is_set():
            try:
                r = self.client.call("pull", {"token": self.token, "since": self.since, "wait": 20, "rv": self.rv, "pts": self.pts,
                                              "view": [0, 0, 1000, 1000]}, timeout=40)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
                continue
            now = time.time()
            if "members" in r:
                self.rv = r["rv"]
            self.pts = r["now"] - 0.5
            for e in r["ops"]:
                if e["fid"] == self.fid and e["author"] == self.id:
                    self.ver = e["ver"]
                elif self.i % 10 == 0:  # every tenth member measures how fast others' changes arrive
                    t = sent_at.get(e["uid"])
                    if t:
                        seen_lat.append(now - t)
            if r["ops"]:
                self.since = r["ops"][-1]["seq"]

    def run_presence(self):
        while not stop.is_set():
            try:
                self.client.call("presence", {"token": self.token, "p": {"c": [random.random() * 1000,
                                                                               random.random() * 1000],
                                                                         "e": [0, 0, 1000, 1000], "edit": ["L1"]}},
                                 timeout=30)
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
            stop.wait(2.0)


if RTT:
    _call = wire.Client.call

    def slow_call(self, *a, **k):
        time.sleep(RTT / 2)
        r = _call(self, *a, **k)
        time.sleep(RTT / 2)
        return r

    wire.Client.call = slow_call


def ram_mb():
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                           "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                           "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

    c = PMC()
    c.cb = ctypes.sizeof(c)
    k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.WorkingSetSize / 2**20, c.PeakWorkingSetSize / 2**20


ram_start = ram_mb()[0]
members = [Member(i) for i in range(N)]
t0 = time.time()
joiners = [threading.Thread(target=m.join) for m in members]
for t in joiners:
    t.start()
while any(t.is_alive() for t in joiners):
    app.processEvents()
    time.sleep(0.01)
print(f"{N} members joined in {time.time() - t0:.1f} s", flush=True)
threads = []
for m in members:
    for fn in (m.run_edits, m.run_pull, m.run_presence):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        threads.append(t)

lag = []
end = time.time() + SECONDS
last = time.perf_counter()
while time.time() < end:
    app.processEvents()
    now = time.perf_counter()
    lag.append(now - last)
    last = now
    time.sleep(0.01)
stop.set()
for _ in range(300):
    app.processEvents()
    time.sleep(0.01)

edits = sum(m.edits for m in members)
x = host.hub.numbers()
p = sorted(seen_lat)


def pct(v, q):
    return v[min(len(v) - 1, int(len(v) * q))] if v else float("nan")


print(f"edits: {edits} ({edits / SECONDS:.1f}/s); stream head {x['head']}")
print(f"seen by another member: median {statistics.median(p) * 1000:.0f} ms, 95% {pct(p, .95) * 1000:.0f} ms, "
      f"max {max(p) * 1000:.0f} ms")
print(f"push round trip: median {statistics.median(push_rt) * 1000:.0f} ms, 95% {pct(sorted(push_rt), .95) * 1000:.0f} ms")
print(f"host write: average {x['apply_ms']:.1f} ms, slowest {x['apply_max']:.1f} ms")
print(f"host window: longest pause {max(lag) * 1000:.0f} ms, 99% {pct(sorted(lag), .99) * 1000:.0f} ms")
ram_now, ram_peak = ram_mb()
print(f"memory of the whole test process (host + {N} simulated members): {ram_start:.0f} MB at start, "
      f"{ram_now:.0f} MB now, peak {ram_peak:.0f} MB")
print(f"host sent {x['bytes_out'] / 1e6:.1f} MB, received {x['bytes_in'] / 1e6:.1f} MB, {x['requests']} requests")
check(not errors, f"no request failed ({len(errors)}: {errors[:3]})")
check(edits > N * SECONDS / 5, "every member kept editing")
final = {f.id(): f["n"] for f in lyr.getFeatures() if f.id() <= N}
check(all(final[m.fid] == m.edits for m in members), "no change was lost: every parcel has its member's last edit")
check(pct(p, .95) < 2.0 + 3 * RTT, "95% of changes reach the others within 2 s (plus the connection's delay)")
check(ram_peak < 2048, "the whole room fits in less than 2 GB of memory")
check(pct(sorted(lag), .99) < 0.25, "the host's window stays smooth (99% of frames within 250 ms)")
stop.set()
session.close_room(host)
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}")
os._exit(1 if failures else 0)
