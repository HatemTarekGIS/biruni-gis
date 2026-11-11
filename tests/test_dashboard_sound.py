"""Share by sound (dashboard/ext_sound.py + sound_modem.py): the PC's join tokens and the guest cards they give
(never more than the presenter's card; expired and used-up tokens refused), the Reed-Solomon code, and the
modem through a simulated room - the PC's own WAV and the page's own synth, resampled 48 -> 44.1 kHz, with
reverb and noise - decoded by the page offscreen.

Run:  engine\\bin\\python-qgis.bat tests\\test_dashboard_sound.py
"""
import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import wave
import io

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp()

from PyQt6.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
BROWSER = "--browser" in sys.argv
failures = []
errors = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


if not BROWSER:
    from qgis.core import QgsApplication, QgsFeature, QgsGeometry, QgsProject, QgsVectorLayer  # noqa: E402

    app = QgsApplication([], True)
    app.initQgis()
    from biruni.dashboard import cards, ext_sound, model, seals, service, sound_modem  # noqa: E402
    from biruni.dashboard import page as P  # noqa: E402

    cards._TEST_SEED[:] = [bytes(range(32))]
    seals._PATH[:] = [os.path.join(tempfile.mkdtemp(), "seals.sqlite")]
    prj = QgsProject.instance()
    lyr = QgsVectorLayer("Point?crs=EPSG:4326&field=district:string&field=owner:string", "parcels", "memory")
    fs = []
    for i in range(40):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(f"POINT({(i % 10) * 0.09} {0.2 + (i % 2)})"))
        f.setAttributes(["North" if i % 2 else "South", "SECRET-%d" % i])
        fs.append(f)
    lyr.dataProvider().addFeatures(fs)
    prj.addMapLayer(lyr)
    D = model.blank("Meeting")
    D["sources"] = {"s1": {"layer": lyr.id(), "filter": ""}}
    D["widgets"] = [{"id": "kpi", "type": "kpi", "title": "Parcels", "x": 0, "y": 0, "w": 3, "h": 2, "source": "s1",
                     "measure": {"op": "count"}},
                    {"id": "tbl", "type": "table", "title": "Rows", "x": 3, "y": 0, "w": 6, "h": 4, "source": "s1",
                     "options": {"fields": ["district", "owner"]}}]
    model.save(prj, D)
    pres_card = cards.issue(D["id"], "Presenter North", widgets=["kpi", "tbl"], hide=["owner"],
                            zone_wkt="POLYGON((-1 0.9,2 0.9,2 2,-1 2,-1 0.9))", days=1)
    svc = service.DashboardService(prj)
    box = []
    ps = svc.connect(box.append, "bridge")
    ps.receive({"t": "sound_token"})
    check(box and box[-1].get("why") == "hello first", "no token before the presenter's hello")
    ps.receive({"t": "hello", "card": pres_card})
    box.clear()
    ps.receive({"t": "sound_token", "minutes": 30})
    tk = box[-1]
    check(tk.get("t") == "sound_token" and len(tk.get("token", "")) == 16 and tk["minutes"] == 30, "a join token")
    gbox = []
    gs = svc.connect(gbox.append, "bridge")
    gs.receive({"t": "sound_join", "token": tk["token"], "lang": "ar"})
    kinds = [m["t"] for m in gbox]
    check(kinds[:3] == ["sound_card", "dash", "data"], f"sound_join: a guest card, then the dashboard ({kinds[:3]})")
    gcard = cards.parse(gbox[0]["card"])
    pcard = cards.parse(pres_card)
    check(gcard["hide"] == pcard["hide"] and gcard["zone"] == pcard["zone"] and gcard["widgets"] == pcard["widgets"],
          "the guest card has the presenter's limits (widgets, hidden fields, zone)")
    check(gcard["exp"] <= time.time() + 30 * 60 + 5, "the guest card lasts the minutes asked, not days")
    check(gbox[2]["widgets"]["kpi"]["value"] == 20 and "SECRET" not in json.dumps(gbox, ensure_ascii=False),
          "the guest sees the presenter's zone (20 of 40) and never the hidden field")
    bbox = []
    bs = svc.connect(bbox.append, "bridge")
    bs.receive({"t": "sound_join", "token": "00" * 8})
    check(bbox and bbox[-1].get("why") == "bad token", "an unknown token is refused")
    ext_sound.TOKENS[tk["token"]]["until"] = time.time() - 1
    xbox = []
    svc.connect(xbox.append, "bridge").receive({"t": "sound_join", "token": tk["token"]})
    check(xbox and xbox[-1].get("why") == "bad token", "an expired token is refused")
    tok2 = ext_sound.new_token(D["id"], minutes=10)
    ext_sound.TOKENS[tok2.hex()]["uses"] = ext_sound.MAX_USES
    ybox = []
    svc.connect(ybox.append, "bridge").receive({"t": "sound_join", "token": tok2.hex()})
    check(ybox and ybox[-1].get("why") == "bad token", "a used-up token is refused")
    zbox = []
    zs = svc.connect(zbox.append, "bridge")
    zs.receive({"t": "sound_join", "token": ext_sound.new_token(D["id"]).hex()})
    zs.receive({"t": "sound_join", "token": ext_sound.new_token(D["id"]).hex()})
    check(zbox[-1].get("t") == "error", "a second sound_join after the hello is not taken")
    for _ in range(10):
        ps.receive({"t": "sound_token"})
    check(box[-1].get("why") == "busy", "at most 10 tokens a minute per presenter")

    # the PC as presenter (the dock's action) with stand-ins for the dock and its sharing
    class _Lan:
        port = 47219
        token = "Ab3dE5fG7hI9jK1L"

    class _Sh:
        lan = _Lan()
        anywhere = None

        def running(self):
            return True

        def addresses(self):
            return ["127.0.0.1", "192.168.1.23"]

    class _Dock:
        sharing = _Sh()

        def current(self):
            return D

        def selected_card(self):
            return None

    wav, payload, tok = ext_sound.invite_audio(_Dock(), 60)
    check(payload[:2] == bytes([1, 2]) and payload[2:6] == bytes([192, 168, 1, 23]) and len(wav) > 100000,
          f"the PC's invitation on this network: {len(payload)} bytes, {len(wav) // 1024} KB of WAV")
    with wave.open(io.BytesIO(wav)) as w:
        sr_pc = w.getframerate()
        pcm = w.readframes(w.getnframes())
    dur = len(pcm) / 2 / sr_pc
    check(1.4 < dur < 2.0, f"the burst lasts {dur:.2f} s")
    bridge_p = sound_modem.bridge_payload(base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode(), bytes(8))
    cfg = {"html": P.page_html("en", "bridge"), "pcm": base64.b64encode(pcm).decode(), "sr": sr_pc,
           "lan_payload": payload.hex(), "lan_token": tok.hex(), "bridge_payload": bridge_p.hex(),
           "block_lan": sound_modem.block(payload).hex(), "block_bridge": sound_modem.block(bridge_p).hex()}
    cfgp = os.path.join(tempfile.mkdtemp(), "cfg.json")
    with open(cfgp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--browser", cfgp], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    for ln in r.stdout.splitlines():
        if ln.startswith(("PASS", "FAIL ", "sound")) or "JS ERROR" in ln:
            print(ln, flush=True)
        if ln.startswith("FAIL "):
            failures.append(ln[5:])
    if "BROWSER DONE" not in r.stdout:
        check(False, "the page part ran to its end (exit %s)" % r.returncode)
        print(r.stdout[-1500:], r.stderr[-2000:])
else:
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
    from PyQt6.QtWidgets import QApplication  # noqa: E402

    app = QApplication(sys.argv[:1])
    cfg = json.load(open(sys.argv[sys.argv.index("--browser") + 1], encoding="utf-8"))

    def wait(cond, sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            app.processEvents()
            try:
                if cond():
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.02)
        return False

    class Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
            if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                errors.append(f"{message} ({line})")
                print("JS ERROR:", message, line, flush=True)

    def js(pg, expr):
        out = {}
        pg.runJavaScript(expr, lambda r: out.update(v=r))
        wait(lambda: "v" in out, 240)
        return out.get("v")

    v = QWebEngineView()
    pg = Page(QWebEngineProfile.defaultProfile())
    v.setPage(pg)
    v.resize(900, 700)
    v.show()
    pg.setHtml(cfg["html"], QUrl("https://hatemtarekgis.github.io/biruni-gis/d/#l=en"))
    wait(lambda: js(pg, "!!window.BiruniSound") is True, 20)
    js(pg, "window.CFG=" + json.dumps(cfg) + ";1")
    check(js(pg, "!!document.getElementById('ext_sound_lis')") is True, "a page without a card offers «Listen to join»")
    TEST = r"""
(()=>{const S=BiruniSound,C=window.CFG,R={};const hex=a=>[...a].map(x=>x.toString(16).padStart(2,'0')).join('');
 const unhex=h=>Uint8Array.from(h.match(/../g).map(x=>parseInt(x,16)));
 R.blockLan=hex(S.block(unhex(C.lan_payload)))===C.block_lan;R.blockBridge=hex(S.block(unhex(C.bridge_payload)))===C.block_bridge;
 /* Reed-Solomon: 8 wrong bytes are corrected; 9 are not taken for the message */
 let rnd=7;const rand=()=>{rnd=(rnd*1103515245+12345)&0x7fffffff;return rnd/0x7fffffff};
 const code=S.block(unhex(C.bridge_payload));let ok8=0,bad9=0;
 for(let t=0;t<40;t++){const c=[...code],pos=new Set();while(pos.size<8)pos.add(Math.floor(rand()*60));for(const p of pos)c[p]^=1+Math.floor(rand()*255);
  const d=S.rsDec(c,16);if(d&&hex(d)===hex(code.slice(0,44)))ok8++;
  const c9=[...code],p9=new Set();while(p9.size<9)p9.add(Math.floor(rand()*60));for(const p of p9)c9[p]^=1+Math.floor(rand()*255);
  const d9=S.rsDec(c9,16);if(!d9||hex(d9)!==hex(code.slice(0,44)))bad9++}
 R.rs={ok8:ok8,bad9:bad9};
 /* the PC's own WAV */
 const b=atob(C.pcm),pcm=new Float32Array(b.length/2);for(let i=0;i<pcm.length;i++){let v=b.charCodeAt(2*i)|(b.charCodeAt(2*i+1)<<8);if(v>32767)v-=65536;pcm[i]=v/32768}
 let t0=performance.now();const r0=S.decodeSamples(pcm,C.sr);R.pcWav={ok:!!r0&&hex(r0.payload)===C.lan_payload,ms:Math.round(performance.now()-t0)};
 const pp=S.parsePayload(unhex(C.lan_payload));R.parseLan=pp&&pp.mode==='lan'&&pp.ip==='192.168.1.23'&&pp.port===47219&&pp.path==='Ab3dE5fG7hI9jK1L'&&pp.token===C.lan_token;
 const pb=S.parsePayload(unhex(C.bridge_payload));R.parseBridge=pb&&pb.mode==='bridge'&&pb.key.length===43;
 /* a room: 48 kHz synth -> 44.1 kHz microphone, reverb, noise, a hum, people talking (random tones) */
 const room=(x,sr0,sr1,snrDb,rt,lead)=>{const n=Math.floor(x.length*sr1/sr0),y=new Float32Array(n);
  for(let i=0;i<n;i++){const p=i*sr0/sr1,k=Math.floor(p),f=p-k;y[i]=(x[k]||0)*(1-f)+(x[k+1]||0)*f}
  const L=Math.round(rt*sr1),ir=new Float32Array(L);ir[0]=1;for(let i=1;i<L;i++)ir[i]=(rand()*2-1)*0.35*Math.exp(-6.9*i/L);
  const z=new Float32Array(n+Math.round(lead*sr1)+L);const o=Math.round(lead*sr1);
  for(let i=0;i<n;i++){const v=y[i];if(!v)continue;for(let j=0;j<L;j+=1)z[o+i+j]+=v*ir[j]}
  let ps=0;for(let i=0;i<z.length;i++)ps+=z[i]*z[i];ps/=z.length;const pn=ps/Math.pow(10,snrDb/10),sd=Math.sqrt(pn);
  for(let i=0;i<z.length;i++){const g=Math.sqrt(-2*Math.log(rand()+1e-12))*Math.cos(2*Math.PI*rand());z[i]+=g*sd*0.8+0.05*Math.sin(2*Math.PI*100*i/sr1)}
  for(let k=0;k<6;k++){const f=300+rand()*2500,a=Math.floor(rand()*z.length),len=Math.round(0.2*sr1);for(let i=0;i<len&&a+i<z.length;i++)z[a+i]+=0.05*Math.sin(2*Math.PI*f*i/sr1)}
  return z};
 const pay=unhex(C.bridge_payload),syn=S.synth(S.block(pay),48000),res=[];
 for(const [snr,rt] of [[20,0.03],[10,0.06],[6,0.08],[3,0.08],[0,0.06],[10,0.25],[6,0.25],[10,0.5],[6,0.5],[10,0.8]]){const z=room(syn,48000,44100,snr,rt,0.37);t0=performance.now();
  const r=S.decodeSamples(z,44100);res.push({snr:snr,rt:rt,ok:!!r&&hex(r.payload)===C.bridge_payload,ms:Math.round(performance.now()-t0)})}
 R.room=res;return JSON.stringify(R)})()"""
    R = json.loads(js(pg, TEST) or "{}")
    print("sound:", json.dumps(R), flush=True)
    check(R.get("blockLan") and R.get("blockBridge"), "the page and the PC make the same 60 bytes (Reed-Solomon)")
    rs = R.get("rs") or {}
    check(rs.get("ok8") == 40, f"Reed-Solomon corrects 8 wrong bytes (40/40; got {rs.get('ok8')})")
    check(rs.get("bad9") == 40, f"9 wrong bytes are never taken for the message (got {rs.get('bad9')}/40)")
    check((R.get("pcWav") or {}).get("ok"), f"the page decodes the PC's own WAV ({(R.get('pcWav') or {}).get('ms')} ms)")
    check(R.get("parseLan") and R.get("parseBridge"), "both kinds of invitation read back (network and anywhere)")
    room = R.get("room") or []
    for x in room:
        print(f"sound room: SNR {x['snr']} dB, reverb {int(x['rt'] * 1000)} ms -> {'decoded' if x['ok'] else 'not decoded'}"
              f" in {x['ms']} ms", flush=True)
    check(all(x["ok"] for x in room if x["snr"] >= 6 and x["rt"] <= 0.25), "decoded through the room down to 6 dB SNR with reverb up to 250 ms (44.1 kHz mic)")
    check(all(x["ok"] for x in room if x["snr"] >= 10 and x["rt"] <= 0.5), "decoded at 10 dB with a 500 ms echo")
    check(not errors, "no JavaScript errors" + ("" if not errors else ": " + "; ".join(errors[:3])))
    v.close()
    print("BROWSER DONE", flush=True)

print("ALL PASSED" if not failures else f"FAILED: {len(failures)}")
sys.exit(1 if failures else 0)
