"""P1 clock skew test:
- phone clock 3 hours ahead sends lpos; PC rejects off-time envelope and replies with signed {t:'ltime', now} (BRLT1|<now>);
- phone verifies BRLT1, updates SKEW, re-sends lpos with NOW(), gets lack, and PC stores the true time;
- photos sealed while skew was unknown carry tu:1 inside and a bounded outer dt correction applied by clean_meta.
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

from biruni.field_photos import bridge, handover, ladder, ladder_page, server  # noqa: E402

failures = []


def check(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}", flush=True)
    if not cond:
        failures.append(msg)


print("=== P1 Clock Skew Test ===", flush=True)
work = tempfile.mkdtemp(prefix="biruni_p1_skew_")

try:
    key = bridge.new_key()
    sk_b64, pin = bridge.identity(key)
    lad = ladder.Ladder(key)
    true_now_ms = int(time.time() * 1000)
    skew_ms = 3 * 3600 * 1000   # phone clock is 3 hours ahead
    phone_clock_ms = true_now_ms + skew_ms

    # 1. Phone (in Node running CORE_JS + LADDER_JS) has Date.now() shifted +3h ahead.
    # First tick sends lpos with ts = phone_clock_ms (+3h).
    # PC Ladder.take rejects it as off-time and returns signed {t:'ltime', now}.
    # Phone receives ltime via SIG.arrived, verifies BRLT1, updates SKEW (~ -3h),
    # re-sends lpos with NOW(), PC accepts it and adjusts position timestamp t to true time!
    env_ahead = lad.seal({
        "t": "lpos", "v": 1, "d": "dev_skew_0001", "e": "ep_skew",
        "lo": 1, "bl": 1,
        "b": [[1, {"id": "dev_skew_0001", "name": "Hatem", "lat": 30.0444, "lon": 31.2357, "acc": 4, "t": phone_clock_ms}]],
        "n": "n_ahead_1", "ts": phone_clock_ms,
    })
    pts_rejected, ltime_sealed = lad.take(env_ahead, "mqtt", server.clean_pos, lambda p: p)
    ltime_obj = lad.open(ltime_sealed) if ltime_sealed else None
    check(pts_rejected == [] and isinstance(ltime_obj, dict) and ltime_obj.get("t") == "ltime"
          and abs(ltime_obj.get("now", 0) - true_now_ms) < 5000,
          f"PC rejects +3h off-time lpos and replies with signed ltime ({ltime_obj})")

    # Run Node simulation of phone receiving ltime, updating SKEW, and re-sending lpos with NOW()
    node = shutil.which("node") or next(
        (p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), "node"
    )
    node_script = os.path.join(work, "skew_flow.js")
    with open(node_script, "w", encoding="utf-8") as f:
        f.write("const crypto = require('crypto').webcrypto; globalThis.crypto = crypto;\n")
        f.write("const REAL_NOW = Date.now;\n")
        f.write(f"Date.now = () => REAL_NOW() + {skew_ms};\n")
        f.write("globalThis.location = {hostname:'example.com', hash:'#k=" + key + "&i=" + pin + "'};\n")
        f.write("const store = {}; globalThis.localStorage = {getItem:k=>store[k]||null, setItem:(k,v)=>{store[k]=String(v)}, removeItem:k=>{delete store[k]}};\n")
        f.write("globalThis.keep = (k,v)=>{if(v==null)delete store[k];else store[k]=String(v)};\n")
        f.write("globalThis.kept = k=>store[k]||null;\n")
        f.write("globalThis.ROOM = '" + key[:12] + "';\n")
        f.write("globalThis.DEVID = 'dev_skew_0001';\n")
        f.write("globalThis.GPSMODE = true;\n")
        f.write("globalThis.T = {};\n")
        f.write("globalThis.$ = () => null;\n")
        f.write("globalThis.lineOpen = () => false;\n")
        f.write("globalThis.WebSocket = class { close(){} };\n")
        f.write("globalThis.fetch = async ()=>({ok:false});\n")
        f.write(bridge._core() + "\n")
        f.write(ladder_page.LADDER_JS + "\n")
        f.write(r"""
(async () => {
 await bridgeKey('""" + key + r"""');
 let gotLtime = false;
 SIG.on = (o, kind) => {
  if (o.t === 'ltime') gotLtime = true;
 };
 /* Phone tries a forged ltime first -> must be refused and not change SKEW */
 const forgedLtime = await seal({t:'ltime', now: REAL_NOW(), n: rid(), ts: REAL_NOW(), ip: 'bad', is: 'bad'});
 await SIG.arrived(forgedLtime, 'mqtt');
 const skewAfterForged = SKEW;
 const refusedAfterForged = SIG.refused || 0;

 /* Now phone receives the genuine signed ltime from PC */
 await SIG.arrived(""" + json.dumps(ltime_sealed) + r""", 'mqtt');
 const skewAfterValid = SKEW;
 const nowCorrected = NOW();

 /* Build the re-sent lpos using NOW() */
 const env2 = {
  t: 'lpos', v: 1, d: DEVID, e: 'ep_skew', lo: 1, bl: 1,
  b: [[1, {id: DEVID, name: 'Hatem', lat: 30.0444, lon: 31.2357, acc: 4, t: Date.now()}]],
  n: rid(), ts: NOW()
 };
 const sealed2 = await seal(env2, true);
 const nostrEv = await nostrEvent('hello', BK.topic);
 console.log(JSON.stringify({
  skewAfterForged, refusedAfterForged, gotLtime, SKEW_OK,
  skewAfterValid, nowDiff: Math.abs(nowCorrected - REAL_NOW()),
  nostrDiff: Math.abs(nostrEv.created_at * 1000 - REAL_NOW()),
  sealed2
 }));
 process.exit(0);
})();
""")
    proc = subprocess.run([node, node_script], capture_output=True, text=True, timeout=15)
    check(proc.returncode == 0, f"Node clock skew simulation ran (rc={proc.returncode}, err={proc.stderr.strip()})")
    sim = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    check(sim.get("skewAfterForged") == 0 and sim.get("refusedAfterForged") == 1,
          f"forged ltime is refused and leaves SKEW=0 ({sim})")
    check(sim.get("gotLtime") is True and sim.get("SKEW_OK") is True
          and abs(sim.get("skewAfterValid", 0) - (-skew_ms)) < 5000
          and sim.get("nowDiff", 999999) < 5000
          and sim.get("nostrDiff", 999999) < 5000,
          f"genuine ltime sets SKEW (~-3h) and NOW() / nostrEvent match real time ({sim})")

    # Now pass sealed2 (re-sent by phone after learning SKEW) to PC Ladder.take
    pts_ok, lack_sealed = lad.take(sim["sealed2"], "mqtt", server.clean_pos, lambda p: p)
    lack_obj = lad.open(lack_sealed) if lack_sealed else None
    check(len(pts_ok) == 1 and isinstance(lack_obj, dict) and lack_obj.get("t") == "lack" and lack_obj.get("upto") == 1,
          f"re-sent lpos with NOW() is accepted by PC and returns lack upto=1 ({lack_obj})")

    # 2. Verify PC stores the true time when a phone's position timestamp `t` was recorded with a skewed clock
    env_drift = lad.seal({
        "t": "lpos", "v": 1, "d": "dev_skew_0001", "e": "ep_skew",
        "lo": 2, "bl": 1,
        "b": [[2, {"id": "dev_skew_0001", "name": "Hatem", "lat": 30.05, "lon": 31.24, "acc": 4, "t": true_now_ms - 120_000}]],
        "n": "n_drift_2", "ts": true_now_ms - 120_000,
    })
    pts_drift, _ = lad.take(env_drift, "mqtt", server.clean_pos, lambda p: p)
    check(len(pts_drift) == 1 and abs(pts_drift[0]["t"] - true_now_ms) < 5000,
          f"Ladder.take adjusts drifted position timestamp t to true PC time ({pts_drift[0]['t']} vs {true_now_ms})")

    # 3. Photos sealed while skew was unknown carry tu:1 inside and bounded outer dt correction
    rec_corrected = server.clean_meta({
        "id": "photo-skew-0001", "lat": 30.0444, "lon": 31.2357,
        "t": phone_clock_ms, "tu": 1, "dt": -skew_ms,
    })
    taken_ts = dt.datetime.fromisoformat(rec_corrected["taken"]).timestamp() * 1000
    check(abs(taken_ts - true_now_ms) < 5000,
          f"clean_meta applies outer dt correction when tu=1 ({rec_corrected['taken']})")

    # Without tu=1, outer dt is ignored (cannot shift time of a photo whose clock was already known)
    rec_no_tu = server.clean_meta({
        "id": "photo-skew-0002", "lat": 30.0444, "lon": 31.2357,
        "t": phone_clock_ms, "dt": -skew_ms,
    })
    taken_no_tu_ts = dt.datetime.fromisoformat(rec_no_tu["taken"]).timestamp() * 1000
    check(abs(taken_no_tu_ts - phone_clock_ms) < 5000,
          "clean_meta ignores outer dt when tu!=1")

    # Outer dt is bounded to +-30 days
    rec_huge_dt = server.clean_meta({
        "id": "photo-skew-0003", "lat": 30.0444, "lon": 31.2357,
        "t": true_now_ms, "tu": 1, "dt": 90 * 24 * 3600 * 1000,
    })
    taken_huge_ts = dt.datetime.fromisoformat(rec_huge_dt["taken"]).timestamp() * 1000
    check(abs((taken_huge_ts - true_now_ms) - 30 * 24 * 3600 * 1000) < 5000,
          "clean_meta clamps outer dt to at most 30 days")

finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
