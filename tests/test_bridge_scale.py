"""Offline tests for scale-A: dead reckoning, batching/stickiness, rendezvous pool, and CSP.

Run:  engine\\bin\\python-qgis.bat tests\\test_bridge_scale.py
"""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

from biruni.field_photos import bridge, ladder_page, relay_pool  # noqa: E402
from biruni.gps import deadreckon  # noqa: E402
from biruni.secure import csp  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


node = shutil.which("node") or next(
    (p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None
)
check(node is not None, "node is available")
work = tempfile.mkdtemp(prefix="biruni-scale-")

# ---- 1. Dead reckoning in Node + Python parity -----------------------------------------------
DR_TEST_JS = ladder_page.DR_JS + r"""
function mulberry32(a){
 return function(){
  let t=a+=0x6D2B79F5;
  t=Math.imul(t^(t>>>15),t|1);t^=t+Math.imul(t^(t>>>7),t|61);
  return ((t^(t>>>14))>>>0)/4294967296;
 };
}
const M_DEG=111320;
function stepLatLon(lat,lon,distM,deg){
 const r=deg*Math.PI/180,k=Math.max(1e-6,Math.abs(Math.cos(lat*Math.PI/180)));
 return [lat+(distM*Math.cos(r))/M_DEG,lon+(distM*Math.sin(r))/(M_DEG*k)];
}
function toXY(lat0,lon0,lat,lon){
 const k=Math.cos(lat0*Math.PI/180);
 return [(lon-lon0)*M_DEG*k,(lat-lat0)*M_DEG];
}
function ptSegDist(p,a,b){
 const dx=b[0]-a[0],dy=b[1]-a[1],l=dx*dx+dy*dy;
 let t=l?((p[0]-a[0])*dx+(p[1]-a[1])*dy)/l:0;
 t=Math.max(0,Math.min(1,t));
 return Math.hypot(p[0]-(a[0]+t*dx),p[1]-(a[1]+t*dy));
}
function runTrack(fixes){
 DR.reset();
 const sent=[],sendTimes=[];
 let sendEvents=0;
 for(const f of fixes){
  const pts=DR.offer(f,!!f.force);
  if(pts.length){sendEvents++;sendTimes.push(f.t);for(const x of pts)sent.push(x)}
 }
 return {sendEvents,sent,sendTimes};
}
const T0=1700000000000;

// 1a. Straight at 1.4 m/s, acc 5, 600 s with seeded noise
{
 const rng=mulberry32(101);
 const fixes=[],truth=[];
 let lat=24.7136,lon=46.6753;
 for(let s=0;s<600;s++){
  truth.push({lat,lon});
  const nLat=(rng()-0.5)*2.0,nLon=(rng()-0.5)*2.0;
  const [fLat,fLon]=stepLatLon(lat+nLat/M_DEG,lon+nLon/(M_DEG*Math.cos(lat*Math.PI/180)),0,0);
  fixes.push({lat:fLat,lon:fLon,acc:5,speed:1.4+(rng()-0.5)*0.1,cog:45+(rng()-0.5)*4,t:T0+s*1000});
  [lat,lon]=stepLatLon(lat,lon,1.4,45);
 }
 const res=runTrack(fixes);
 const sentXY=res.sent.map(p=>toXY(24.7136,46.6753,p.lat,p.lon));
 let maxErr=0;
 for(const tr of truth){
  const p=toXY(24.7136,46.6753,tr.lat,tr.lon);
  let best=Infinity;
  for(let i=0;i<sentXY.length-1;i++)best=Math.min(best,ptSegDist(p,sentXY[i],sentXY[i+1]));
  if(best>maxErr)maxErr=best;
 }
 console.log(JSON.stringify({tag:'1a',sends:res.sendEvents,maxErr}));
}

// 1b. 90-degree turn every 60 s over 600 s
{
 const rng=mulberry32(202);
 const fixes=[];
 let lat=24.7136,lon=46.6753,hdg=0;
 for(let s=0;s<600;s++){
  if(s>0&&s%60===0)hdg=(hdg+90)%360;
  const nLat=(rng()-0.5)*1.2/M_DEG,nLon=(rng()-0.5)*1.2/(M_DEG*Math.cos(lat*Math.PI/180));
  fixes.push({lat:lat+nLat,lon:lon+nLon,acc:5,speed:1.6,cog:hdg,t:T0+s*1000});
  [lat,lon]=stepLatLon(lat,lon,1.6,hdg);
 }
 const res=runTrack(fixes);
 const turnOk=[];
 for(let k=60;k<600;k+=60){
  const tTurn=T0+k*1000;
  turnOk.push(res.sendTimes.some(st=>st>=tTurn&&st<=tTurn+3000));
 }
 console.log(JSON.stringify({tag:'1b',allTurnsCaught:turnOk.every(Boolean),turnOk}));
}

// 1c. 60 s moving / 120 s stopped cycle over 600 s
{
 const rng=mulberry32(303);
 const fixes=[];
 let lat=24.7136,lon=46.6753;
 for(let s=0;s<600;s++){
  const phase=s%180,moving=phase<60;
  const nLat=(rng()-0.5)*1.5/M_DEG,nLon=(rng()-0.5)*1.5/(M_DEG*Math.cos(lat*Math.PI/180));
  fixes.push({lat:lat+nLat,lon:lon+nLon,acc:5,speed:moving?1.5:0.0,cog:moving?90:null,t:T0+s*1000});
  if(moving)[lat,lon]=stepLatLon(lat,lon,1.5,90);
 }
 const res=runTrack(fixes);
 // Count sends strictly inside stopped windows (phase 61..179, excluding the stop transition at 60)
 const stoppedWindows=[[60,180],[240,360],[420,540]];
 const stoppedSends=stoppedWindows.map(([a,b])=>{
  return res.sendTimes.filter(t=>t>T0+a*1000&&t<T0+b*1000).length;
 });
 const transitions=stoppedWindows.map(([a,b])=>
  res.sendTimes.includes(T0+a*1000)&&res.sendTimes.includes(T0+b*1000));
 console.log(JSON.stringify({tag:'1c',stoppedSends,transitions}));
}

// 1d. Straight at 15 m/s, 600 s
{
 const rng=mulberry32(404);
 const fixes=[];
 let lat=24.7136,lon=46.6753;
 for(let s=0;s<600;s++){
  const nLat=(rng()-0.5)*2.0/M_DEG,nLon=(rng()-0.5)*2.0/(M_DEG*Math.cos(lat*Math.PI/180));
  fixes.push({lat:lat+nLat,lon:lon+nLon,acc:5,speed:15.0,cog:120,t:T0+s*1000});
  [lat,lon]=stepLatLon(lat,lon,15.0,120);
 }
 const res=runTrack(fixes);
 console.log(JSON.stringify({tag:'1d',sends:res.sendEvents}));
}

// 1e. force always sends, plus predict() vectors for Python comparison
{
 DR.reset();
 const p0={lat:24.7136,lon:46.6753,acc:5,speed:3.2,cog:37.5,t:T0};
 DR.offer(p0,true);
 const preds=[];
 for(const dt of [0,1,4.5,10,15]){
  const q=DR.predict(T0+dt*1000);
  preds.push({dt,lat:q.lat,lon:q.lon});
 }
 const f1=DR.offer({lat:24.7136,lon:46.6753,acc:5,speed:0,cog:null,t:T0+500},true);
 const f2=DR.offer({lat:24.7136,lon:46.6753,acc:5,speed:0,cog:null,t:T0+600},true);
 console.log(JSON.stringify({tag:'1e',f1:f1.length,f2:f2.length,preds}));
}
"""

p_dr = os.path.join(work, "dr.js")
with open(p_dr, "w", encoding="utf-8") as fh:
    fh.write(DR_TEST_JS)
r_dr = subprocess.run([node, p_dr], capture_output=True, text=True, encoding="utf-8", timeout=30)
check(r_dr.returncode == 0, "DR Node script exited cleanly: " + r_dr.stderr[-200:])
dr_out = {d["tag"]: d for d in (json.loads(line) for line in r_dr.stdout.strip().splitlines())}
check(dr_out["1a"]["sends"] <= 45 and dr_out["1a"]["maxErr"] <= 20.0,
      f"1.4 m/s straight: sends={dr_out['1a']['sends']}<=45, maxErr={dr_out['1a']['maxErr']:.2f}m<=20m")
check(dr_out["1b"]["allTurnsCaught"], f"90-deg turns caught within 3s: {dr_out['1b']['turnOk']}")
check(all(n <= 4 for n in dr_out["1c"]["stoppedSends"]) and all(dr_out["1c"]["transitions"]),
      f"60s moving / 120s stopped: stoppedSends={dr_out['1c']['stoppedSends']}, "
      f"transitions={dr_out['1c']['transitions']}")
check(dr_out["1d"]["sends"] <= 70, f"15 m/s straight: sends={dr_out['1d']['sends']}<=70")
check(dr_out["1e"]["f1"] == 1 and dr_out["1e"]["f2"] == 1, "force always sends")

py_match = True
for pr in dr_out["1e"]["preds"]:
    plat, plon = deadreckon.predict(24.7136, 46.6753, 3.2, 37.5, pr["dt"])
    if abs(plat - pr["lat"]) >= 1e-7 or abs(plon - pr["lon"]) >= 1e-7:
        py_match = False
check(py_match, "Python deadreckon.predict equals JS DR.predict to < 1e-7 deg")

# ---- 2. Batching in Node with stubbed globals and fake clock ----------------------------------
LAD_TEST_JS = r"""
let fakeNow=1000000,nextTid=1;
const intervals=new Map();
globalThis.Date=class extends Date{constructor(...a){super(...(a.length?a:[fakeNow]))}static now(){return fakeNow}};
globalThis.setInterval=(fn,ms)=>{const id=nextTid++;intervals.set(id,{fn,ms,next:fakeNow+ms});return id};
globalThis.clearInterval=id=>intervals.delete(id);
globalThis.setTimeout=(fn)=>0;
globalThis.clearTimeout=()=>{};

let directUp=false,mqttUp=false,nostrUp=false,ownUp=false;
let sentLog=[];
const ROOM='room1',DEVID='device_123456',GPSMODE=true,GDRIVE=false,TUNH='',T={},ID={pin:null};
const BR={batchMs:10000,primaryTtl:90000};
let BK={topic:'topic1'};
const $=()=>null,kept=()=>null,keep=()=>{},offline=()=>{};
let _rid=0;const rid=()=>'r'+(++_rid);
const seal=async obj=>JSON.stringify(obj);
const IDOK=async()=>true;
const lineOpen=()=>directUp;
const lineText=s=>{const m=JSON.parse(s);sentLog.push({t:fakeNow,kind:'direct',env:JSON.parse(m.s)})};
const SIG={
 relays:[],
 kinds:()=>({mqtt:mqttUp?1:0,nostr:nostrUp?1:0,own:ownUp?1:0}),
 sendRaw:(s,kind)=>{sentLog.push({t:fakeNow,kind,env:JSON.parse(s)});return true}
};
""" + ladder_page.LADDER_JS + r"""
async function flush(){for(let i=0;i<15;i++)await Promise.resolve()}
async function runTimers(){
 for(const it of [...intervals.values()]){
  if(fakeNow>=it.next){it.next=fakeNow+it.ms;await it.fn();await flush()}
 }
}
function resetLad(){
 sentLog=[];
 LAD._items=[];LAD._pushes=0;LAD._waitSince=0;LAD._lastProbe=0;LAD._lastRelayProbe=0;
 LAD.seq=0;LAD.upto=0;LAD.cap=true;LAD._ticking=false;LAD._startedAt=fakeNow;
 for(const c of LAD.channels){c.okAt=0;c.fails=0;c.unacked=0;c.waitMs=0;c.until=0;c.lastSent=-1}
}
async function ackPending(){
 for(const s of sentLog){
  if(s._acked)continue;
  s._acked=true;
  const hi=s.env.b[s.env.b.length-1][0];
  await LAD.onAck({d:DEVID,e:LAD.epoch,upto:hi},s.kind);
  await flush();
 }
}

(async()=>{
 await LAD.init();await flush();

 // 2a. mqtt primary, 60 pushes in 60 s
 fakeNow=1000000;resetLad();
 directUp=false;mqttUp=true;nostrUp=false;ownUp=false;
 const mqttCh=LAD.channels.find(c=>c.id==='mqtt');
 mqttCh.okAt=fakeNow;
 const seenSeqs=new Set();
 for(let s=0;s<60;s++){
  fakeNow=1000000+s*1000;
  await runTimers();
  await LAD.push({lat:24.7+s*1e-5,lon:46.6,acc:5,t:fakeNow});await flush();
  await ackPending();
 }
 fakeNow=1060000;await LAD.tick();await flush();await ackPending();
 const maxPts=Math.max(0,...sentLog.map(x=>x.env.b.length));
 const allSeqs=sentLog.flatMap(x=>x.env.b.map(p=>p[0]));
 const uniqueSeqs=new Set(allSeqs);
 console.log(JSON.stringify({tag:'2a',envs:sentLog.length,maxPts,backlog:LAD.backlog,
  upto:LAD.upto,uniqueCount:uniqueSeqs.size,totalPoints:allSeqs.length}));

 // 2b. direct primary: >= 50 envelopes for 60 pushes in 60 s
 fakeNow=2000000;resetLad();
 directUp=true;mqttUp=true;nostrUp=false;ownUp=false;
 LAD.channels.find(c=>c.id==='direct').okAt=fakeNow;
 for(let s=0;s<60;s++){
  fakeNow=2000000+s*1000;
  await runTimers();
  await LAD.push({lat:24.7+s*1e-5,lon:46.6,acc:5,t:fakeNow});await flush();
  await ackPending();
 }
 console.log(JSON.stringify({tag:'2b',envs:sentLog.length,backlog:LAD.backlog}));

 // 2c. after a silent gap of 30 s, only the primary is used
 fakeNow=3000000;resetLad();
 directUp=false;mqttUp=true;nostrUp=true;ownUp=true;
 // nostr is primary, own and mqtt are higher relay channels that are ready
 LAD.channels.find(c=>c.id==='nostr').okAt=fakeNow;
 LAD._lastRelayProbe=fakeNow;
 for(let s=1;s<=29;s++){fakeNow=3000000+s*1000;await runTimers()}
 sentLog=[];
 fakeNow=3030000;
 await LAD.push({lat:24.75,lon:46.65,acc:5,t:fakeNow});await flush();
 console.log(JSON.stringify({tag:'2c',kinds:sentLog.map(x=>x.kind)}));

 // 2d. backlog of 200 drains at the 2 s pace
 fakeNow=4000000;resetLad();
 directUp=false;mqttUp=false;nostrUp=false;ownUp=false;
 for(let i=0;i<200;i++){
  await LAD.push({lat:24.7+i*1e-5,lon:46.6,acc:5,t:fakeNow+i*100});await flush();
 }
 const blBefore=LAD.backlog;
 mqttUp=true;
 LAD.channels.find(c=>c.id==='mqtt').okAt=fakeNow;
 for(let s=0;s<20&&LAD.backlog>0;s++){
  fakeNow=4000000+s*1000;
  await LAD.tick();await flush();
  for(const e of sentLog){
   if(e._acked)continue;e._acked=true;
   // Simulate Ladder.take contiguous upto
   const qs=e.env.b.map(p=>p[0]).sort((a,b)=>a-b);
   let u=LAD.upto;
   for(const q of qs){if(q===u+1)u=q}
   await LAD.onAck({d:DEVID,e:LAD.epoch,upto:u},e.kind);await flush();
  }
 }
 const times=sentLog.map(x=>x.t);
 const gaps=[];for(let i=1;i<times.length;i++)gaps.push(times[i]-times[i-1]);
 console.log(JSON.stringify({tag:'2d',blBefore,blAfter:LAD.backlog,envs:sentLog.length,gaps}));
})();
"""

p_lad = os.path.join(work, "lad.js")
with open(p_lad, "w", encoding="utf-8") as fh:
    fh.write(LAD_TEST_JS)
r_lad = subprocess.run([node, p_lad], capture_output=True, text=True, encoding="utf-8", timeout=30)
check(r_lad.returncode == 0, "LAD Node script exited cleanly: " + r_lad.stderr[-200:])
lad_out = {d["tag"]: d for d in (json.loads(line) for line in r_lad.stdout.strip().splitlines())}
check(lad_out["2a"]["envs"] <= 7 and lad_out["2a"]["maxPts"] <= 60
      and lad_out["2a"]["uniqueCount"] == 60 and lad_out["2a"]["totalPoints"] == 60
      and lad_out["2a"]["backlog"] == 0,
      f"mqtt primary 60 pushes: {lad_out['2a']}")
check(lad_out["2b"]["envs"] >= 50 and lad_out["2b"]["backlog"] == 0,
      f"direct primary 60 pushes: {lad_out['2b']}")
check(lad_out["2c"]["kinds"] == ["nostr"],
      f"after 30 s silent gap, only primary used: {lad_out['2c']}")
check(lad_out["2d"]["blBefore"] == 200 and lad_out["2d"]["blAfter"] == 0
      and all(g == 2000 for g in lad_out["2d"]["gaps"]),
      f"backlog of 200 drains at 2 s pace: {lad_out['2d']}")

# ---- 3. Rendezvous hashing --------------------------------------------------------------------
topics_20 = [f"biruni_topic_{i:02d}" for i in range(20)]
py_ranks = {t: relay_pool.ranked(t, relay_pool.NOSTR) for t in topics_20}
JS_RANK = bridge._core() + r"""
(async()=>{
 const topics=JSON.parse(process.argv[2]);
 const urls=JSON.parse(process.argv[3]);
 const out={};
 for(const t of topics)out[t]=await rankUrls(t,urls);
 console.log(JSON.stringify(out));
})();
"""
p_rk = os.path.join(work, "rank.js")
with open(p_rk, "w", encoding="utf-8") as fh:
    fh.write(JS_RANK)
r_rk = subprocess.run(
    [node, p_rk, json.dumps(topics_20), json.dumps(list(relay_pool.NOSTR))],
    capture_output=True, text=True, encoding="utf-8", timeout=30,
)
check(r_rk.returncode == 0 and json.loads(r_rk.stdout or "{}") == py_ranks,
      "Python and JS rankings match for 20 topics")

pool_12 = [f"wss://relay-{i:02d}.example.org" for i in range(12)]
counts = {u: 0 for u in pool_12}
for i in range(10000):
    for u in relay_pool.subset(f"t_{i}", pool_12, 3):
        counts[u] += 1
# Expected share of top 3 out of 12 = 3/12 = 25% (2500 hits); within +-10% relative => [2250, 2750]
shares_ok = all(0.225 <= c / 10000 <= 0.275 for c in counts.values())
check(shares_ok, f"10,000 topics over 12 URLs: top-3 shares within +-10% of 25% "
      f"(min={min(counts.values())/100:.2f}%, max={max(counts.values())/100:.2f}%)")

removed = pool_12[5]
pool_11 = [u for u in pool_12 if u != removed]
only_held_changed = True
for i in range(500):
    t = f"rm_{i}"
    s12 = relay_pool.subset(t, pool_12, 3)
    s11 = relay_pool.subset(t, pool_11, 3)
    if removed not in s12 and s12 != s11:
        only_held_changed = False
        break
check(only_held_changed, "removing one URL changes only the subsets that held it")

subset_ok = True
for t in topics_20:
    for kind, urls in (("nostr", relay_pool.NOSTR), ("mqtt", relay_pool.MQTT)):
        phone_s = set(relay_pool.subset(t, urls, relay_pool.K[kind]["phone"]))
        host_s = set(relay_pool.subset(t, urls, relay_pool.K[kind]["host"]))
        if not (phone_s <= host_s):
            subset_ok = False
check(subset_ok, "phone subset always lies inside host subset")

one_ok = all(relay_pool.subset(f"one_{i}", ["wss://only.example.org"], 3) == ["wss://only.example.org"]
             for i in range(20))
check(one_ok, "a one-URL pool is always chosen")

# ---- 4. CSP: with_csp and site_policies for d/ and s/ list every pool origin, no wildcard ----
csp_html = bridge.with_csp(bridge.phone_page({"en": {}}))
conn_tokens = csp_html.split("connect-src", 1)[1].split(";", 1)[0].split()
pol = csp.site_policies()
pool_origins = [f"{urllib.parse.urlsplit(u).scheme}://{urllib.parse.urlsplit(u).netloc}"
                for u in (*relay_pool.NOSTR, *relay_pool.MQTT, bridge.OWN)]
if bridge.NTFY:
    p_n = urllib.parse.urlsplit(bridge.NTFY)
    pool_origins.extend([f"{p_n.scheme}://{p_n.netloc}", f"ws{'s' if p_n.scheme == 'https' else ''}://{p_n.netloc}"])

check(all(o in conn_tokens for o in pool_origins), "with_csp connect-src includes every pool origin")
check(all("*" not in tok or "trycloudflare.com" in tok for tok in conn_tokens),
      "with_csp has no wildcard except tunnel")
for path_key in ("d/index.html", "s/index.html"):
    ct = pol[path_key]["connect"]
    check(all(o in ct for o in pool_origins) and all("*" not in tok for tok in ct),
          f"site_policies()[{path_key!r}] lists every pool origin with no wildcard")

# ---- 5. SIG subsetting, health check, cog wiring, link_text, probe_relays syntax --------------
SIG_TEST_JS = r"""
let fakeNow=50000;
globalThis.Date=class extends Date{constructor(...a){super(...(a.length?a:[fakeNow]))}static now(){return fakeNow}};
const sockets=[];
globalThis.WebSocket=class{
 constructor(url,proto){this.url=url;this.proto=proto;this.readyState=0;sockets.push(this)}
 send(){}
 close(){this.readyState=3;if(this.onclose)this.onclose({})}
};
""" + "const location={hostname:process.argv[2],hash:process.argv[3]};\n" + bridge._core() + r"""
(async()=>{
 await bridgeKey(process.argv[4]);
 SIG.start(()=>{},()=>{});
 const nostr=SIG.relays.filter(r=>r.kind==='nostr').map(r=>r.url);
 const mqtt=SIG.relays.filter(r=>r.kind==='mqtt').map(r=>r.url);
 // Trigger health check at +9s for nostr[0]
 const firstN=SIG.relays.find(r=>r.kind==='nostr');
 fakeNow+=9000;
 SIG._health();
 const afterN=SIG.relays.filter(r=>r.kind==='nostr').map(r=>r.url);
 console.log(JSON.stringify({hostside:HOSTSIDE,poolv:POOLV,nostr,mqtt,
  replaced:firstN.closed&&!afterN.includes(firstN.url),afterN}));
 clearInterval(SIG._hc);process.exit(0);
})();
"""
p_sig = os.path.join(work, "sig.js")
with open(p_sig, "w", encoding="utf-8") as fh:
    fh.write(SIG_TEST_JS)
k_test = bridge.new_key()
for label, hname, hhash, exp_n_fn in (
    ("host", "127.0.0.1", f"#k={k_test}", lambda o, n: len(n) >= 5 and set(o[:5]) <= set(n)),
    ("phone_p2", "photos.biruni-gis.com", f"#k={k_test}&p=2", lambda o, n: n == o[:3]),
    ("legacy", "photos.biruni-gis.com", f"#k={k_test}", lambda o, n: set(n) == {u for u in relay_pool.LEGACY_NOSTR if u in relay_pool.NOSTR}),   # dead legacy dropped
):
    rs = subprocess.run([node, p_sig, hname, hhash, k_test], capture_output=True, text=True, encoding="utf-8",
                        timeout=20)
    sd = json.loads(rs.stdout.strip()) if rs.returncode == 0 else {}
    order_n = relay_pool.ranked(
        "biruni" + __import__("hashlib").sha256(
            b"biruni-bridge-1:" + __import__("base64").urlsafe_b64decode(k_test + "=")
        ).hexdigest()[:40],
        relay_pool.NOSTR,
    )
    check(rs.returncode == 0 and exp_n_fn(order_n, sd.get("nostr", [])),
          f"SIG.start ({label}) nostr subset & health check (replaced={sd.get('replaced')})")
    if label != "legacy":
        check(sd.get("replaced") is True and len(sd.get("afterN", [])) == len(sd.get("nostr", [])),
              f"SIG._health ({label}) closes and replaces dead relay with next ranked")

from biruni.field_photos.server import clean_pos  # noqa: E402
from biruni.gps.browser import fix_from_json  # noqa: E402
from biruni.tracking import link_text  # noqa: E402

cp = clean_pos({"id": "device_1234", "lat": 24.7, "lon": 46.6, "heading": 180, "cog": 95})
check(cp["heading"] == 180 and cp["cog"] == 95, "clean_pos preserves both heading and cog")
fx1 = fix_from_json({"lat": 24.7, "lon": 46.6, "heading": 180, "cog": 95})
fx2 = fix_from_json({"lat": 24.7, "lon": 46.6, "heading": 180})
check(fx1.course == 95 and fx2.course == 180, "fix_from_json uses cog when present else heading")
lt1 = link_text({"relays": {"hosts": ["a.io", "b.io", "c.io", "d.io", "e.io", "f.io"]}})
check("a.io, b.io, c.io, d.io +2" in lt1, f"link_text formats 4 hosts +2 ({lt1!r})")

shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
