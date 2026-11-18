"""Unit tests for the GPS fallback ladder: MQTT wire codec, Ladder seal/dedupe/ack,
/x/ endpoint + rate limit, _local() proxy header guard, TrackRecorder late inserts,
and link_text().

Run:  engine\\bin\\python-qgis.bat tests\\test_gps_ladder.py
"""
import base64
import datetime as dt
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

app = QgsApplication([], False)
app.initQgis()

from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from biruni.field_photos import bridge  # noqa: E402
from biruni.field_photos.ladder import Ladder, b64u  # noqa: E402
from biruni.field_photos.server import PhotoServer, clean_pos, phone_texts  # noqa: E402
from biruni.gps.nmea import Fix  # noqa: E402
from biruni.gps.recorder import TrackRecorder  # noqa: E402
from biruni.tracking import link_text  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


work = tempfile.mkdtemp(prefix="biruni-gps-lad-")
node = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None)

# ---- 1. MQ byte vectors + split parsing + JS seal <-> Python Ladder + IDOK -------------------
key = bridge.new_key()
lad = Ladder(key)
pin = bridge.pin(key)
now_ms = int(time.time() * 1000)

if node:
    js_mq = bridge._core() + r"""
(async()=>{
 const c = MQ.connect('b1234567890123456');
 const sub = MQ.subscribe(7, 'biruni/1/abc');
 const p0 = MQ.publish('t', 'hi', 0, 0);
 const p1 = MQ.publish('t', 'x'.repeat(200), 1, 42);
 const png = MQ.ping();
 // split parsing across chunks (including mid-varint and multi-packet)
 const combined = cat(c, sub, p0, p1, png);
 const got = [];
 let buf = new Uint8Array(0);
 let cur = 0;
 for (const chunkLen of [3, 5, 19, 130, 500]) {
  const part = combined.slice(cur, cur + chunkLen);
  cur += chunkLen;
  buf = cat(buf, part);
  const res = MQ.parse(buf);
  got.push(...res.pk.map(x => [x.type, x.flags, x.body.length]));
  buf = res.rest;
 }
 await bridgeKey(process.argv[2]);
 const env = {t:'lpos', v:1, d:'device_1234', e:'epoch1', lo:1, bl:1,
  b:[[1, {id:'device_1234', lat:24.71, lon:46.67, acc:5, t:Number(process.argv[3])}]],
  n:'n1', ts:Number(process.argv[3])};
 const sealed = await seal(env, true);
 console.log(JSON.stringify({
  c: [...c], sub: [...sub], p0: [...p0], p1Head: [...p1.slice(0, 6)], png: [...png],
  got, rest: buf.length, sealed, topic: BK.topic
 }));
})();
"""
    p = os.path.join(work, "mq.js")
    with open(p, "w", encoding="utf-8") as f:
        f.write(js_mq)
    r = subprocess.run([node, p, key, str(now_ms)], capture_output=True, text=True, encoding="utf-8")
    check(r.returncode == 0, "node MQ & seal script ran " + r.stderr[-200:])
    d = json.loads(r.stdout or "{}")
    # CONNECT('b1234567890123456'): 10 1f 00 04 'MQTT' 04 02 00 3c 00 11 'b1234567890123456'
    exp_c = [0x10, 29, 0, 4, *b"MQTT", 4, 2, 0, 60, 0, 17, *b"b1234567890123456"]
    check(d.get("c") == exp_c, "MQ.connect exact wire bytes")
    exp_sub = [0x82, 17, 0, 7, 0, 12, *b"biruni/1/abc", 0]
    check(d.get("sub") == exp_sub, "MQ.subscribe exact wire bytes")
    exp_p0 = [0x30, 5, 0, 1, ord("t"), ord("h"), ord("i")]
    check(d.get("p0") == exp_p0, "MQ.publish QoS 0 exact wire bytes")
    # p1: topic 't' (3) + pid 42 (2) + 200 bytes = 205 -> varint [205 & 127 | 128, 1] = [0xCD, 0x01]
    check(d.get("p1Head") == [0x32, 0xCD, 0x01, 0, 1, ord("t")], "MQ.publish QoS 1 2-byte varint")
    check(d.get("png") == [0xC0, 0x00], "MQ.ping exact wire bytes")
    check(d.get("got") == [[1, 0, 29], [8, 2, 17], [3, 0, 5], [3, 2, 205], [12, 0, 0]] and d.get("rest") == 0,
          f"MQ.parse handles split frames & multi-packet buffers ({d.get('got')})")
    check(d.get("topic") == lad.topic, "Python Ladder.topic matches JS BK.topic")

    js_sealed = d["sealed"]
    pos_list, ack_sealed = lad.take(js_sealed, "mqtt", clean_pos, lambda x: x)
    check(len(pos_list) == 1 and pos_list[0]["seq"] == 1 and pos_list[0]["via"] == "mqtt",
          "Python Ladder opens JS-sealed compressed lpos envelope")
    check(ack_sealed is not None, "Ladder produces sealed ack")

    js_verify = bridge._core() + r"""
(async()=>{
 await bridgeKey(process.argv[2]);
 const h_bak = location.hash;
})();
"""
    # Verify Python ack in node using IDOK + unseal
    js_ack_check = (
        "const location={hash:'#k=' + process.argv[2] + '&i=' + process.argv[3]};\n"
        + bridge._core()
        + r"""
(async()=>{
 await bridgeKey(process.argv[2]);
 const ack = await unseal(process.argv[4]);
 const ok = await IDOK(ack.ip, ack.is, 'BRLA1|' + ack.d + '|' + ack.e + '|' + ack.upto);
 const bad = await IDOK(ack.ip, ack.is, 'BRLA1|' + ack.d + '|' + ack.e + '|' + (ack.upto + 1));
 console.log(JSON.stringify({ack, ok, bad}));
})();
"""
    )
    p2 = os.path.join(work, "ack.js")
    with open(p2, "w", encoding="utf-8") as f:
        f.write(js_ack_check)
    r2 = subprocess.run([node, p2, key, pin, ack_sealed], capture_output=True, text=True, encoding="utf-8")
    d2 = json.loads(r2.stdout or "{}") if r2.returncode == 0 else {}
    check(d2.get("ok") is True and d2.get("bad") is False and d2.get("ack", {}).get("upto") == 1,
          f"Python ack passes JS IDOK and rejects altered upto ({d2})")
else:
    print("SKIP node not found")

# ---- 2. Ladder: duplicates, lo, late, wrong key, 2 MB inflate bomb ---------------------------
lad2 = Ladder(key)
dev = "dev_test_001"
ep = "ep_01"
t1 = now_ms - 10_000
t2 = now_ms - 5_000
t3 = now_ms - 2_000

env1 = lad2.seal({
    "t": "lpos", "v": 1, "d": dev, "e": ep, "lo": 1, "bl": 2,
    "b": [
        [2, {"id": dev, "lat": 24.712, "lon": 46.672, "t": t2}],
        [1, {"id": dev, "lat": 24.711, "lon": 46.671, "t": t1}],
    ],
    "n": "m1", "ts": now_ms,
})
p_out, ack1 = lad2.take(env1, "mqtt", clean_pos, lambda x: x)
check([p["seq"] for p in p_out] == [1, 2], "Ladder delivers out-of-order batch in seq order")
check(p_out[0].get("late") is True and not p_out[1].get("late"), "older t in batch is flagged late")
check(lad2.open(ack1)["upto"] == 2, "ack upto advances to 2")

# Duplicate replay
p_dup, ack_dup = lad2.take(env1, "nostr", clean_pos, lambda x: x)
check(p_dup == [] and lad2.last_info["dups"] == 2 and lad2.open(ack_dup)["upto"] == 2,
      "duplicate envelope produces no second positions and reports dups=2")

# Gap then lo jumps over gap: seq 5 arrives -> upto stays 2; then lo=5 -> upto becomes 5
env_gap = lad2.seal({
    "t": "lpos", "v": 1, "d": dev, "e": ep, "lo": 1, "bl": 1,
    "b": [[5, {"id": dev, "lat": 24.715, "lon": 46.675, "t": t3}]],
    "n": "m2", "ts": now_ms,
})
p_gap, ack_gap = lad2.take(env_gap, "tunnel", clean_pos, lambda x: x)
check(len(p_gap) == 1 and lad2.open(ack_gap)["upto"] == 2, "gap holds upto at 2")

env_lo = lad2.seal({
    "t": "lpos", "v": 1, "d": dev, "e": ep, "lo": 5, "bl": 0,
    "b": [[4, {"id": dev, "lat": 24.714, "lon": 46.674, "t": t2}]],
    "n": "m3", "ts": now_ms,
})
p_lo, ack_lo = lad2.take(env_lo, "tunnel", clean_pos, lambda x: x)
check(p_lo == [] and lad2.open(ack_lo)["upto"] == 5,
      "lo=5 advances upto past gap and through buffered seq 5, ignoring old seq 4")

# Wrong key
other_lad = Ladder(bridge.new_key())
check(other_lad.open(env1) is None and other_lad.take(env1, "mqtt", clean_pos, lambda x: x) == ([], None),
      "wrong key is refused")

# 2 MB inflate bomb refused
comp = zlib.compressobj(9, zlib.DEFLATED, -15)
bomb = comp.compress(b'{"x":"' + b"a" * (2 * 1024 * 1024) + b'"}') + comp.flush()
raw_k = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
iv = b"\x01" * 12
ct = AESGCM(raw_k).encrypt(iv, b"\x01" + bomb, None)
check(lad2.open(b64u(iv + ct)) is None, "2 MB inflate payload is refused (1 MB cap)")

# ---- 3. PhotoServer /x/ + _local() proxy header guard --------------------------------------
port = free_port()
srv = PhotoServer(os.path.join(work, "photos"), os.path.join(work, "cert"), {"en": phone_texts(lambda s: s)},
                  "en", (), port, ["127.0.0.1"], bind="127.0.0.1")
srv.set_ladder(key)
ctx = ssl._create_unverified_context()


def req(method, path, body=b"", headers=None):
    h = {"Connection": "close"}
    if headers:
        h.update(headers)
    c = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=5)
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    hdrs = dict(r.getheaders())
    c.close()
    return r.status, data, hdrs


st, _, _ = req("POST", "/x/wrongtopic", b"abc")
check(st == 404, "/x/ wrong topic gives 404")

st, _, hdrs = req("POST", f"/x/{srv.ladder.topic}", b"bad-seal")
check(st == 403 and hdrs.get("Access-Control-Allow-Origin") == "*",
      "/x/ bad seal gives 403 with CORS header")

# 29 more bad seals -> total 30 failures -> next one is 429
codes_30 = [req("POST", f"/x/{srv.ladder.topic}", b"bad-seal")[0] for _ in range(29)]
st_31, _, _ = req("POST", f"/x/{srv.ladder.topic}", b"bad-seal")
check(all(c == 403 for c in codes_30) and st_31 == 429,
      f"/x/ returns 403 for 30 failures, then 429 after 30 (got {st_31})")

# Reset rate block to test replay on /x/
srv._x_block_until = 0.0
srv._x_fails.clear()
while not srv.events.empty():
    srv.events.get_nowait()

env_x = srv.ladder.seal({
    "t": "lpos", "v": 1, "d": dev, "e": "epx", "lo": 1, "bl": 1,
    "b": [[1, {"id": dev, "lat": 24.72, "lon": 46.68, "t": now_ms}]],
    "n": "nx1", "ts": now_ms,
})
st_ok1, body1, _ = req("POST", f"/x/{srv.ladder.topic}?a=0", env_x.encode("utf-8"))
ans1 = json.loads(body1)
st_ok2, body2, _ = req("POST", f"/x/{srv.ladder.topic}?a={ans1['a']}", env_x.encode("utf-8"))
evs = []
while not srv.events.empty():
    evs.append(srv.events.get_nowait())
pos_evs = [e for e in evs if e[0] == "pos"]
check(st_ok1 == 200 and st_ok2 == 200 and len(ans1["m"]) == 1 and len(pos_evs) == 1,
      f"/x/ delivers ack in m and replay gives no second pos event (pos_evs={len(pos_evs)})")

# _local() refuses requests with Cloudflare/proxy headers
st_loc, _, _ = req("GET", f"/{srv.token}/bridge-config")
st_cf, _, _ = req("GET", f"/{srv.token}/bridge-config", headers={"Cf-Connecting-Ip": "1.2.3.4"})
st_ray, _, _ = req("GET", f"/{srv.token}/bridge-config", headers={"Cf-Ray": "abc"})
st_xff, _, _ = req("GET", f"/{srv.token}/bridge-config", headers={"X-Forwarded-For": "1.2.3.4"})
check(st_loc == 200 and st_cf == 404 and st_ray == 404 and st_xff == 404,
      f"_local() refuses proxy headers ({st_loc},{st_cf},{st_ray},{st_xff})")
srv.stop()

# ---- 4. TrackRecorder: late points in time order, distance, duplicates, before-t0 ------------
rec = TrackRecorder()
rec.start("late-test", "bridge")
base_t = dt.datetime.now(dt.timezone.utc)


def mkfix(dt_s, lat, lon):
    return Fix(time=base_t + dt.timedelta(seconds=dt_s), lat=lat, lon=lon, alt=None,
               speed_kmh=None, course=None, hdop=1.0, quality=1, valid=True, sentences={"browser"})


# Live points at 0s and 20s (~111m/deg? ~0.001 deg lat ~ 110.8m)
check(rec.add(mkfix(0, 30.000, 31.000)) is True, "recorder accepts point at t=0s")
check(rec.add(mkfix(20, 30.002, 31.000)) is True, "recorder accepts point at t=20s")
d_straight = rec.distance_m
# Late point at 10s off to the east -> increases distance, lands between 0s and 20s
check(rec.add(mkfix(10, 30.001, 31.001)) is True, "recorder accepts late point at t=10s")
check([int(round((p[0] - base_t).total_seconds())) for p in rec.points] == [0, 10, 20],
      "late point lands in time order")
check(rec.distance_m > d_straight + 30,
      f"distance adjusted across three segments ({d_straight:.1f} -> {rec.distance_m:.1f})")
# Duplicate within 0.5s dropped
check(rec.add(mkfix(10.2, 30.005, 31.005)) is False, "duplicate within 0.5s dropped")
# Point > 5s before t0 dropped
check(rec.add(mkfix(-10, 30.003, 31.003)) is False, "fix > 5s before t0 dropped")
st_trk = rec.stop()
check(st_trk is not None and st_trk.points == 3 and abs(st_trk.distance_m - rec.distance_m) < 0.5,
      f"recorder stop() updates track line with all 3 ordered points (dist={st_trk.distance_m:.1f})")

# ---- 5. link_text ---------------------------------------------------------------------------
check(link_text({}) == "", "link_text empty for empty dict")
check("Adding a direct address" in link_text({"tunnel_adding": True}), "link_text shows tunnel adding")
check("scan the code again" in link_text({"tunnel": "abc", "qr": "https://x/#k=1&t=abc"}),
      "link_text shows direct address ready")
txt_mqtt = link_text({"via": "mqtt", "backlog": 12})
check("Public relay (MQTT)" in txt_mqtt and "12" in txt_mqtt, f"link_text shows mqtt + backlog ({txt_mqtt!r})")
txt_tun = link_text({"via": "tunnel", "backlog": 0})
check("Direct address (tunnel)" in txt_tun and "Catching up" not in txt_tun,
      "link_text shows tunnel without catching up when backlog=0")

shutil.rmtree(work, ignore_errors=True)
app.exitQgis()
print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
