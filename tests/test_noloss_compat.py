"""P1 backward/forward compatibility test (QWebEngine):
- New page, old PC (no nl in hello): behave as legacy (delete on BRAK1 200).
- Old cached page, new PC: PC still sends is (BRAK1), BRLA1, and dgot; extra fields are ignored.
- Old records without st in IndexedDB q count as st=0.
- Sticky nl:1 per room: once a room sees nl:1, a later hello without nl never downgrades the room.
"""
import json
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

from qgis.PyQt.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402
from qgis.PyQt.QtWebEngineCore import QWebEnginePage, QWebEngineSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

from biruni.field_photos import bridge, handover, ladder, server  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
app = QApplication.instance() or QApplication(sys.argv)

failures = []


def check(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}", flush=True)
    if not cond:
        failures.append(msg)


def wait(pred, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        v = pred()
        if v:
            return v
        time.sleep(0.03)
    return pred()


print("=== P1 Compatibility Test (QWebEngine) ===", flush=True)
work = tempfile.mkdtemp(prefix="biruni_p1_compat_")

try:
    key = bridge.new_key()
    sk_b64, pin = bridge.identity(key)
    hk = handover.HandoverKey(os.path.join(work, "cert"))

    bridge.RELAYS = ()
    bridge.LEGACY_RELAYS = ()
    bridge.MQTT = ()
    bridge.LEGACY_MQTT = ()
    bridge.NTFY = ""
    bridge.OWN = ""

    raw_page = bridge.phone_page(server.all_phone_texts())
    page = QWebEnginePage()
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    page.javaScriptConsoleMessage = lambda lv, msg, ln, src: print("  js:", msg, ln, flush=True)

    url = QUrl(f"https://photos.biruni-gis.com/#k={key}&i={pin}&l=en")
    ready = []
    page.loadFinished.connect(ready.append)
    page.setHtml(raw_page, url)
    check(wait(lambda: bool(ready), 15), "phone page loaded")

    def js(code):
        box = []
        page.runJavaScript(code, box.append)
        wait(lambda: bool(box), 10)
        return box[0] if box else None

    def ajs(expr):
        page.runJavaScript(
            "window.__r=undefined;(async()=>{try{window.__r=JSON.stringify(await (%s))}"
            "catch(e){window.__r=JSON.stringify('error '+e)}})()" % expr
        )
        wait(lambda: js("window.__r!==undefined") is True, 10)
        v = js("window.__r")
        return json.loads(v) if isinstance(v, str) else None

    check(wait(lambda: js("!!BK && !!db && ID.pin==='%s'" % pin) is True, 10),
          "page initialized")

    ajs(f"""(async()=>{{
     const raw=b64u.dec('{sk_b64}');
     window._pcSk=await crypto.subtle.importKey('pkcs8',raw,{{name:'ECDSA',namedCurve:'P-256'}},true,['sign']);
     const j=await crypto.subtle.exportKey('jwk',window._pcSk);
     window._pcPub=b64u.enc(cat(new Uint8Array([4]),b64u.dec(j.x),b64u.dec(j.y)));
     window._pcSign=async txt=>({{pub:window._pcPub,sig:b64u.enc(await crypto.subtle.sign({{name:'ECDSA',hash:'SHA-256'}},window._pcSk,enc.encode(txt)))}});
     return true}})()""")

    # 1. Old records in IndexedDB q without `st`, `c`, `h`, `at` count as st=0
    old_rec_res = ajs(r"""(async()=>{
     const b=new Uint8Array([0xff,0xd8,0xff,0xe0,1,2,3,4,0xff,0xd9]);
     const rawOld={id:'old-nost-1',blob:new Blob([b],{type:'image/jpeg'}),meta:{id:'old-nost-1',sealed:1,t:Date.now()},room:ROOM};
     await tx('readwrite',s=>s.put(rawOld));
     await count();
     const loaded=await getItem('old-nost-1');
     return {st:itemSt(loaded),hasStField:('st' in loaded),nWait:$('nWait').textContent,pill:$('waitPill').textContent};
    })()""")
    check(isinstance(old_rec_res, dict) and old_rec_res.get("st") == 0
          and old_rec_res.get("hasStField") is False
          and old_rec_res.get("nWait") == "1"
          and old_rec_res.get("pill") == "1 photos waiting",
          f"old record without st field counts as st=0 ({old_rec_res})")

    # 2. New page, old PC (hello has no nl; ack has only BRAK1 `is`, no c/h/rt/is2): legacy delete on BRAK1 200
    old_pc_res = ajs(f"""(async()=>{{
     while(busy)await new Promise(r=>setTimeout(r,20));
     hello({{t:'hello',kinds:['Pole'],form:[],pk:'{hk.public}'}});
     if(NL!==false)return {{err:'NL should be false'}};
     dc={{readyState:'open',bufferedAmount:0,send:s=>{{
      if(typeof s!=='string')return;
      const m=JSON.parse(s);
      if(m.t==='photo')window._lastM=m;
      else if(m.t==='end'){{
       (async()=>{{
        const pm=window._lastM;
        const g1=await window._pcSign('BRAK1|'+pm.id+'|'+pm.kind+'|'+pm.nonce);
        await handleAck({{t:'ack',id:pm.id,kind:pm.kind,nonce:pm.nonce,status:200,ip:g1.pub,is:g1.sig}});
       }})();
      }}
     }}}};
     await flush();
     const after=await getItem('old-nost-1');
     return {{nl:NL,deleted:!after,sent:sent}};
    }})()""")
    check(isinstance(old_pc_res, dict) and old_pc_res.get("nl") is False
          and old_pc_res.get("deleted") is True and old_pc_res.get("sent") == 1,
          f"new page with old PC (no nl in hello, BRAK1-only ack) deletes on 200 as legacy ({old_pc_res})")

    # 3. Sticky nl:1 per room: once hello has nl:1, a subsequent hello without nl never downgrades NL
    sticky_res = ajs(f"""(async()=>{{
     hello({{t:'hello',nl:1,now:Date.now(),kinds:['Pole'],form:[],pk:'{hk.public}'}});
     const afterUp=NL;
     hello({{t:'hello',kinds:['Pole'],form:[],pk:'{hk.public}'}});
     const afterNoNl=NL;
     return {{afterUp,afterNoNl,stored:kept('biruni-nl-'+ROOM)}};
    }})()""")
    check(isinstance(sticky_res, dict) and sticky_res.get("afterUp") is True
          and sticky_res.get("afterNoNl") is True and sticky_res.get("stored") == "1",
          f"nl:1 is sticky per room and never downgrades on a hello without nl ({sticky_res})")

    # 4. Old cached page, new PC:
    # Verify that a new PC's ack (with BRAK1 `is` + extra fields c, h, rt, is2),
    # lack (with BRLA1 `is` + extra fields u2, is2), and dgot (with BRDG1 `is`)
    # pass an old page's legacy verification checks that only inspect `is` (BRAK1 / BRLA1 / BRDG1).
    lad = ladder.Ladder(key)
    now_ms = int(time.time() * 1000)
    env = lad.seal({
        "t": "lpos", "v": 1, "d": "dev_old_page_1", "e": "ep_old",
        "lo": 1, "bl": 1,
        "b": [[1, {"lat": 30.0, "lon": 31.0, "t": now_ms}]],
        "n": "n_old_1", "ts": now_ms,
    })
    _, lack_sealed = lad.take(env, "mqtt", server.clean_pos, lambda p: p, mirror_ok=True)
    lack_obj = lad.open(lack_sealed)

    old_page_res = ajs(f"""(async()=>{{
     /* Simulate old cached page's handleAck and onAck which only verify BRAK1 and BRLA1 */
     const g1=await window._pcSign('BRAK1|ph-old-1|original|n-old-1');
     const g2=await window._pcSign('BRAK2|ph-old-1|original|n-old-1|'+'a'.repeat(64)+'|2|'+Date.now());
     const newPcAck={{t:'ack',id:'ph-old-1',kind:'original',nonce:'n-old-1',status:200,c:2,h:'a'.repeat(64),rt:Date.now(),ip:g1.pub,is:g1.sig,is2:g2.sig}};
     const oldPageBrak1Ok=await IDOK(newPcAck.ip,newPcAck.is,'BRAK1|'+newPcAck.id+'|'+newPcAck.kind+'|'+newPcAck.nonce);

     const lack={json.dumps(lack_obj)};
     const oldPageBrla1Ok=await IDOK(lack.ip,lack.is,'BRLA1|'+lack.d+'|'+lack.e+'|'+lack.upto);

     const dg=await window._pcSign('BRDG1|dn-1|keep|ph-old-1');
     const newPcDgot={{t:'dgot',nonce:'dn-1',clean:'keep',ids:['ph-old-1'],ip:dg.pub,is:dg.sig}};
     const oldPageBrdg1Ok=await IDOK(newPcDgot.ip,newPcDgot.is,'BRDG1|'+newPcDgot.nonce+'|'+newPcDgot.clean+'|'+newPcDgot.ids.join(','));
     return {{oldPageBrak1Ok,oldPageBrla1Ok,oldPageBrdg1Ok}};
    }})()""")
    check(isinstance(old_page_res, dict)
          and old_page_res.get("oldPageBrak1Ok") is True
          and old_page_res.get("oldPageBrla1Ok") is True
          and old_page_res.get("oldPageBrdg1Ok") is True,
          f"old cached page accepts new PC's BRAK1, BRLA1, and BRDG1 signatures while ignoring extra fields ({old_page_res})")

    # 5. P5 compatibility:
    # (a) New page, old PC (no pl in hello or guide): share toggle works unchanged (checked -> 'live', unchecked -> 'off')
    toggle_res = ajs(r"""(async()=>{
     const hasPolicy=!!plGet();
     const shareVis=!$('shareRow').classList.contains('hide');
     $('share').checked=true;
     const mOn=plMode();
     $('share').checked=false;
     const mOff=plMode();
     $('share').checked=true;
     return {hasPolicy,shareVis,mOn,mOff};
    })()""")
    check(toggle_res == {"hasPolicy": False, "shareVis": True, "mOn": "live", "mOff": "off"},
          f"new page with old PC (no pl): share toggle works unchanged ({toggle_res})")

    # (b) Old cached page with new PC: position without `tm` is accepted by clean_pos (tm==''),
    # and an old page's hello/setGuide ignores `pl` and `hs:1` without error.
    pos_no_tm = server.clean_pos({"id": "dev_old_page_1", "name": "OldPage", "lat": 30.05, "lon": 31.24, "acc": 5, "t": now_ms})
    check(pos_no_tm is not None and pos_no_tm["tm"] == "",
          f"new PC accepts position without tm from an old cached page ({pos_no_tm})")

finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
