"""Viewers relay to viewers (dashboard/ext_mesh.py): the PC signs a mesh session's messages; phones verify the
signature, the card and the order before they apply or pass anything on - ordering, dedupe, gaps, newcomers,
fan-out, a tampered message, another card's message and a forged key are all checked in a page offscreen; then
three real WebRTC peers in one page pass the dashboard on (one gets it through another).

Run:  engine\\bin\\python-qgis.bat tests\\test_dashboard_mesh.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

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
    from biruni.dashboard import cards, model, seals, service  # noqa: E402
    from biruni.dashboard import ext_mesh  # noqa: E402,F401
    from biruni.dashboard import page as P  # noqa: E402
    from biruni.secure import crypto  # noqa: E402

    SEED = bytes(range(32))
    cards._TEST_SEED[:] = [SEED]
    seals._PATH[:] = [os.path.join(tempfile.mkdtemp(), "seals.sqlite")]
    prj = QgsProject.instance()
    lyr = QgsVectorLayer("Point?crs=EPSG:4326&field=district:string&field=status:string", "parcels", "memory")
    fs = []
    for i in range(60):
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt(f"POINT({(i % 10) * 0.1} {(i // 10) * 0.1})"))
        f.setAttributes(["North" if i % 2 else "South", "open"])
        fs.append(f)
    lyr.dataProvider().addFeatures(fs)
    prj.addMapLayer(lyr)
    D = model.blank("Survey")
    D["sources"] = {"s1": {"layer": lyr.id(), "filter": ""}}
    D["widgets"] = [{"id": "kpi", "type": "kpi", "title": "Done", "x": 0, "y": 0, "w": 3, "h": 2, "source": "s1",
                     "measure": {"op": "count", "where": "status = 'done'"}},
                    {"id": "bar", "type": "bar", "title": "By district", "x": 3, "y": 0, "w": 4, "h": 3, "source": "s1",
                     "measure": {"op": "count"}, "group": {"field": "district"}}]
    model.save(prj, D)
    card1 = cards.issue(D["id"], "Viewers A")
    card2 = cards.issue(D["id"], "Viewers B")
    svc = service.DashboardService(prj)

    # a plain session keeps plain messages
    pbox = []
    ps = svc.connect(pbox.append, "bridge")
    ps.receive({"t": "hello", "card": card1})
    check([m["t"] for m in pbox] == ["dash", "data"], "a session without mesh_hello gets plain dash and data")
    prev = svc.connect([].append, "preview", definition=D)
    pv = []
    prev.send = pv.append
    prev.receive({"t": "hello", "card": None})
    prev.receive({"t": "mesh_hello"})
    check(pv and pv[-1].get("why") == "no mesh", "the builder's preview has no mesh")

    box = []
    s1 = svc.connect(box.append, "bridge")
    s1.receive({"t": "hello", "card": card1})
    box.clear()
    s1.receive({"t": "mesh_hello"})
    envs = [m for m in box if m.get("t") == "mesh_env"]
    check(len(envs) == 2 and not [m for m in box if m.get("t") in ("dash", "data")],
          "after mesh_hello the PC sends signed envelopes only (dash + full data)")
    inner = [json.loads(e["b"]) for e in envs]
    check(inner[0]["m"]["t"] == "dash" and inner[1]["m"]["t"] == "data" and inner[1]["full"] and inner[0]["card"]
          == cards.parse(card1)["id"], "the envelopes carry the card id, the session and 'full'")
    check(all(crypto.verify(bytes.fromhex(cards.public_hex()), bytes.fromhex(e["sg"]), e["b"].encode()) for e in envs),
          "every envelope verifies with the dashboard key")
    dash_env, full1 = envs
    lyr.startEditing()
    ids = [f.id() for f in lyr.getFeatures()]
    idx = lyr.fields().indexOf("status")
    deltas = []
    for k in range(3):
        box.clear()
        lyr.changeAttributeValue(ids[k], idx, "done")
        svc.flush()
        deltas += [m for m in box if m.get("t") == "mesh_env"]
    check(len(deltas) == 3 and all(not json.loads(e["b"])["full"] for e in deltas), "each edit: one signed delta")
    revs = [json.loads(e["b"])["m"]["rev"] for e in deltas]
    check(revs == [revs[0], revs[0] + 1, revs[0] + 2] and revs[0] == json.loads(full1["b"])["m"]["rev"] + 1,
          f"the deltas follow the full state rev by rev ({revs})")
    box.clear()
    s1._mesh_at = 0
    s1.receive({"t": "mesh_full"})
    full2 = [m for m in box if m.get("t") == "mesh_env"]
    check(len(full2) == 1 and json.loads(full2[0]["b"])["full"], "mesh_full: a full state again")
    box.clear()
    s1.receive({"t": "mesh_full"})
    check(not box, "a second mesh_full right away is ignored (rate limit)")
    box2 = []
    s2 = svc.connect(box2.append, "bridge")
    s2.receive({"t": "hello", "card": card2})
    s2.receive({"t": "mesh_hello"})
    other = [m for m in box2 if m.get("t") == "mesh_env"]
    # a forger with another key: its own dash with its own key, signed by itself
    fseed = bytes(range(100, 132))
    fd = json.loads(dash_env["b"])
    fd["m"]["pub"] = crypto.public_key(fseed).hex()
    fbody = json.dumps(fd, separators=(",", ":"))
    forged = {"t": "mesh_env", "b": fbody, "sg": crypto.sign(fseed, fbody.encode()).hex()}
    tb = deltas[1]["b"].replace('"value":', '"value":9', 1)
    tampered = {"t": "mesh_env", "b": tb, "sg": deltas[1]["sg"]}
    check(tb != deltas[1]["b"], "a tampered copy differs")
    cfg = {"html": P.page_html("en", "bridge"), "card1": card1, "card2": card2, "dash": dash_env, "full1": full1,
           "d": deltas, "full2": full2[0], "other": other, "forged": forged, "tampered": tampered}
    cfgp = os.path.join(tempfile.mkdtemp(), "cfg.json")
    with open(cfgp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False)
    lyr.rollBack()
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--browser", cfgp], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    for ln in r.stdout.splitlines():
        if ln.startswith(("PASS", "FAIL", "  ")) or "JS ERROR" in ln or ln.startswith("mesh"):
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
        wait(lambda: "v" in out, 20)
        return out.get("v")

    v = QWebEngineView()
    pg = Page(QWebEngineProfile.defaultProfile())
    v.setPage(pg)
    v.resize(900, 700)
    v.show()
    pg.setHtml(cfg["html"], QUrl("https://biruni-gis.com/d/#l=en"))
    wait(lambda: js(pg, "!!(window.BiruniMesh&&window.BiruniEd)") is True, 20)
    js(pg, "window.CFG=" + json.dumps(cfg, ensure_ascii=False) + ";1")

    SCENARIO = r"""
(async()=>{const {MeshNode}=BiruniMesh,C=window.CFG,R={};const tick=()=>new Promise(r=>setTimeout(r,0));
 const mk=(id,o)=>{const n=new MeshNode(Object.assign({id:id,card:C.card1,apply:m=>{(n.got=n.got||[]).push(m)}},o||{}));return n};
 const link=(up,down)=>{const ok=up.addDown(down.id,m=>setTimeout(()=>down.onPeer(up.id,m),0));down.setUp(up.id,m=>setTimeout(()=>up.onPeer(down.id,m),0),up.hops+1);return ok};
 let asked=0;const A=mk('A',{askPc:()=>{asked++}});A.setUp('pc',null,0);
 const B=mk('B'),Cn=mk('C'),Dn=mk('D');link(A,B);link(A,Cn);link(B,Dn);
 for(const e of [C.dash,C.full1,...C.d])await A.receive(e,'pc');
 for(let i=0;i<40;i++)await tick();await B.q;await Cn.q;await Dn.q;for(let i=0;i<20;i++)await tick();await Dn.q;
 R.order=[A,B,Cn,Dn].map(n=>n.stats.applied);R.lastD=Dn.last&&Dn.last.rev;R.lastA=A.last&&A.last.rev;
 R.dataD=Dn.got.filter(m=>m.t==='data').map(m=>m.rev);
 await A.receive(C.d[2],'pc');R.dupsA=A.stats.dups;
 const before=B.stats.applied;await A.receive(C.tampered,'pc');for(let i=0;i<20;i++)await tick();await B.q;
 R.tamper={rejA:A.stats.rejected,why:A.lastReject,Bsame:B.stats.applied===before};
 await A.receive(C.other[0],'pc');R.other={why:A.lastReject};
 const E=mk('E');await E.receive(C.forged,'pc');R.forged={why:E.lastReject,applied:E.stats.applied};
 const E2=mk('E2',{card:C.card2});await E2.receive(C.dash,'pc');R.card2={why:E2.lastReject};
 /* a gap: F misses the first delta, asks for a full state, then follows again */
 let askF=0;const F=mk('F',{askPc:()=>{askF++}});F.setUp('pc',null,0);
 for(const e of [C.dash,C.full1,C.d[1]])await F.receive(e,'pc');R.gap={asked:askF,last:F.last.rev};
 await F.receive(C.full2,'pc');R.gap.after=F.last.rev;R.gap.full2=JSON.parse(C.full2.b).m.rev;
 /* a gap further down asks upstream */
 const G=mk('G'),H=mk('H');link(A,G);await A.q;for(let i=0;i<30;i++)await tick();await G.q;
 R.newcomer={rev:G.last&&G.last.rev,applied:G.stats.applied,A:A.last.rev};
 R.fanout={added:A.addDown('X1',()=>{}),again:A.addDown('X2',()=>{}),size:A.down.size};
 G.onPeer('A',{t:'env',env:C.full2});await G.q;R.relayedFull=G.last.rev;
 const askedBefore=asked;A.lastNeed=0;A.onPeer('G',{t:'need'});R.needUp=asked-askedBefore;
 window.R1=R})();1"""
    js(pg, SCENARIO)
    wait(lambda: js(pg, "!!window.R1") is True, 30)
    R = json.loads(js(pg, "JSON.stringify(window.R1)") or "{}")
    print("mesh logic:", json.dumps(R), flush=True)
    n_msgs = 2 + len(cfg["d"])
    check(R.get("order") == [n_msgs] * 4, f"A, B, C and D (two hops) each applied dash + full + 3 deltas ({R.get('order')})")
    check(R.get("lastD") == R.get("lastA") and R.get("dataD") == sorted(R.get("dataD") or []),
          "the far viewer ends at the same rev, in order")
    check(R.get("dupsA", 0) >= 1, "a message seen twice is applied once")
    t = R.get("tamper") or {}
    check(t.get("why") == "sig" and t.get("Bsame"), "a tampered message is refused and never passed on")
    check((R.get("other") or {}).get("why") == "card", "another card's envelope (rightly signed) is refused")
    check((R.get("forged") or {}).get("why") == "key" and R["forged"]["applied"] == 0,
          "a forger's own key is refused: the viewer's card was not signed by it")
    check((R.get("card2") or {}).get("why") == "card", "a viewer of card B cannot take card A's stream")
    g = R.get("gap") or {}
    check(g.get("asked") == 1 and g.get("after") == g.get("full2"), "a gap asks for a full state, which repairs it")
    nc = R.get("newcomer") or {}
    check(nc.get("rev") == nc.get("A") and nc.get("applied", 0) >= 2, "a newcomer gets dash + the chain from its relay")
    fo = R.get("fanout") or {}
    check(fo.get("size") == 4 and fo.get("again") is False, "fan-out is limited (4 per viewer)")
    check(R.get("needUp") == 1, "a 'need' from below is passed up to the PC")

    # ---- real WebRTC: three viewers in one page, a fake signalling bus; P serves only one, so R goes through Q ----
    RTC = r"""
(async()=>{const {MeshNode,MeshNet}=BiruniMesh,C=window.CFG,out={};const subs=[];
 const bus=()=>{const b={onmsg:null,send:o=>{const s=JSON.stringify(o);for(const x of subs)if(x!==b)setTimeout(()=>x.onmsg&&x.onmsg(JSON.parse(s)),5)}};subs.push(b);return b};
 const mk=(id,fan)=>new MeshNode({id:id,card:C.card1,fanout:fan,apply:m=>{}});
 const P=mk('P',1),Q=mk('Q',4),R=mk('R',4);P.setUp('pc',null,0);
 for(const e of [C.dash,C.full1,C.d[0]])await P.receive(e,'pc');
 const opt={ice:[],every:400,gather:800};const nP=new MeshNet(P,bus(),opt),nQ=new MeshNet(Q,bus(),opt),nR=new MeshNet(R,bus(),opt);
 const t0=Date.now();while(Date.now()-t0<25000&&!(Q.has()&&R.has()))await new Promise(r=>setTimeout(r,200));
 out.ms=Date.now()-t0;out.Q={up:Q.up,has:Q.has(),rev:Q.last&&Q.last.rev};out.R={up:R.up,hops:R.hops,has:R.has(),rev:R.last&&R.last.rev};
 await P.receive(C.d[1],'pc');const t1=Date.now();while(Date.now()-t1<8000&&!(R.last&&R.last.rev===P.last.rev))await new Promise(r=>setTimeout(r,100));
 out.after={P:P.last.rev,Q:Q.last&&Q.last.rev,R:R.last&&R.last.rev};out.ms2=Date.now()-t1;
 nP.stop();nQ.stop();nR.stop();window.R2=out})();1"""
    js(pg, RTC)
    wait(lambda: js(pg, "!!window.R2") is True, 60)
    R2 = json.loads(js(pg, "JSON.stringify(window.R2)") or "{}")
    print("mesh webrtc:", json.dumps(R2), flush=True)
    check((R2.get("Q") or {}).get("up") == "P" and (R2.get("Q") or {}).get("has"), "WebRTC: Q gets the dashboard from P")
    check((R2.get("R") or {}).get("up") == "Q" and (R2.get("R") or {}).get("hops") == 3 and (R2.get("R") or {}).get("has"),
          "WebRTC: P serves only one, so R gets it through Q (third in line from the PC)")
    a = R2.get("after") or {}
    check(a.get("R") == a.get("P") == a.get("Q"), f"WebRTC: a new change reaches the far viewer ({R2.get('ms2')} ms)")
    check(not errors, "no JavaScript errors" + ("" if not errors else ": " + "; ".join(errors[:3])))
    v.close()
    print("BROWSER DONE", flush=True)

print("ALL PASSED" if not failures else f"FAILED: {len(failures)}")
sys.exit(1 if failures else 0)
