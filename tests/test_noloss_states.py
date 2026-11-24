"""P1 phone item states test (QWebEngine):
- an office ack with c=1 leaves the item at st=2;
- cgot with c=2 deletes it;
- a wrong h or a bad signature keeps it;
- a Drive upload keeps it at st=1;
- a 403 sets st=9 and keeps it;
- end-of-day sheet appears when pending items exist (st < 3 or st === 9).
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

from biruni.field_photos import bridge, handover, server  # noqa: E402

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


print("=== P1 Phone Item States Test (QWebEngine) ===", flush=True)
work = tempfile.mkdtemp(prefix="biruni_p1_states_")

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

    url = QUrl(f"https://photos.biruni-gis.com/#k={key}&i={pin}&l=en&d=1&pk={hk.public}")
    ready = []
    page.loadFinished.connect(ready.append)
    page.setHtml(raw_page, url)
    check(wait(lambda: bool(ready), 15), "phone page loaded in QWebEngine")

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
          "bridgeKey, IndexedDB, and identity pin initialized")

    # Import PC signing key into a helper on window.__pcSign so tests can craft signed PC messages
    ajs(f"""(async()=>{{
     const raw=b64u.dec('{sk_b64}');
     window._pcSk=await crypto.subtle.importKey('pkcs8',raw,{{name:'ECDSA',namedCurve:'P-256'}},true,['sign']);
     const j=await crypto.subtle.exportKey('jwk',window._pcSk);
     window._pcPub=b64u.enc(cat(new Uint8Array([4]),b64u.dec(j.x),b64u.dec(j.y)));
     window._pcSign=async txt=>({{pub:window._pcPub,sig:b64u.enc(await crypto.subtle.sign({{name:'ECDSA',hash:'SHA-256'}},window._pcSk,enc.encode(txt)))}});
     return true}})()""")

    # 1. hello with nl:1 sets sticky NL=true
    ajs("""(async()=>{
     hello({t:'hello',nl:1,now:Date.now(),eod:18,kinds:['Pole'],form:[],pk:PCKEY});
     return NL})()""")
    check(js("NL === true && kept('biruni-nl-'+ROOM) === '1'") is True,
          "hello with nl:1 enables sticky NL capability flag per room")

    # 2. A Drive upload keeps the item in IndexedDB q at st=1 (never deletes on 1 cloud copy)
    drv_res = ajs(r"""(async()=>{
     const b=new Uint8Array([0xff,0xd8,0xff,0xe0,1,2,3,4,0xff,0xd9]);
     const it=await put({id:'p-drive-1',blob:new Blob([b],{type:'image/jpeg'}),meta:{id:'p-drive-1',lat:30,lon:31,t:Date.now()},room:ROOM});
     thumb(it,badgeFor(it));
     window.dupload=async x=>({id:x.id,room:'r',n:1,name:'000001.bfp',size:100,fsha:'aa',st:'q',fid:'fid-1'});
     DRV.on=true;DRV.tok='t';DRV.exp=Date.now()+3600000;DRV.fold={t:'r',r:'root',p:'p'};
     await drivePump();
     const rec=await getItem('p-drive-1');
     const bdg=document.querySelector('[data-id="p-drive-1"] .badge');
     return {exists:!!rec,st:rec&&rec.st,c:rec&&rec.c,h:rec&&rec.h,badge:bdg&&bdg.textContent,pill:$('waitPill').textContent};
    })()""")
    check(isinstance(drv_res, dict) and drv_res.get("exists") is True
          and drv_res.get("st") == 1 and drv_res.get("c") == 1
          and drv_res.get("badge") == "☁️" and len(drv_res.get("h") or "") == 64,
          f"a Drive upload keeps the item in IndexedDB at st=1, c=1, badge='☁️' ({drv_res})")
    check(ajs("dfits(await getItem('p-drive-1'))") is False,
          "item at st=1 is not re-uploaded by Drive")

    # 3. An office ack with c=1 leaves the item at st=2 (never deletes on 1 office copy)
    off1_res = ajs(r"""(async()=>{
     DRV.on=false;
     while(busy)await new Promise(r=>setTimeout(r,20));
     const b=new Uint8Array([0xff,0xd8,0xff,0xe0,5,6,7,8,0xff,0xd9]);
     const it=await put({id:'p-office-1',blob:new Blob([b],{type:'image/jpeg'}),meta:{id:'p-office-1',lat:30,lon:31,t:Date.now()},room:ROOM});
     thumb(it,badgeFor(it));
     dc={readyState:'open',bufferedAmount:0,send:s=>{
      if(typeof s!=='string')return;
      const m=JSON.parse(s);
      if(m.t==='photo')window._lastPhotoMsg=m;
      else if(m.t==='end'){
       (async()=>{
        const pm=window._lastPhotoMsg,rt=Date.now();
        const g1=await window._pcSign('BRAK1|'+pm.id+'|'+pm.kind+'|'+pm.nonce);
        const g2=await window._pcSign('BRAK2|'+pm.id+'|'+pm.kind+'|'+pm.nonce+'|'+it.h+'|1|'+rt);
        await handleAck({t:'ack',id:pm.id,kind:pm.kind,nonce:pm.nonce,status:200,c:1,h:it.h,rt:rt,ip:g1.pub,is:g1.sig,is2:g2.sig});
       })();
      }
     }};
     await flush();
     const rec=await getItem('p-office-1');
     const bdg=document.querySelector('[data-id="p-office-1"] .badge');
     return {exists:!!rec,st:rec&&rec.st,c:rec&&rec.c,badge:bdg&&bdg.textContent,sent:sent,pill:$('waitPill').textContent};
    })()""")
    check(isinstance(off1_res, dict) and off1_res.get("exists") is True
          and off1_res.get("st") == 2 and off1_res.get("c") == 1
          and off1_res.get("badge") == "✓" and off1_res.get("sent") == 1,
          f"an office ack with c=1 leaves the item in IndexedDB at st=2, c=1, badge='✓' ({off1_res})")

    # Verify flush() does NOT re-send st=1 or st=2 items
    resent = ajs(r"""(async()=>{
     while(busy)await new Promise(r=>setTimeout(r,20));
     window._sentIds=[];
     const origSend=sendItem;
     window.sendItem=async x=>{window._sentIds.push(x.id);return origSend(x)};
     await flush();
     window.sendItem=origSend;
     return window._sentIds;
    })()""")
    check(resent == [], f"items with st >= 1 are not re-sent on subsequent flush() ({resent})")

    # 4. A wrong h or a bad signature in ack or cgot is refused and keeps the item
    bad_res = ajs(r"""(async()=>{
     const b=new Uint8Array([0xff,0xd8,0xff,0xe0,9,10,11,12,0xff,0xd9]);
     const it=await put({id:'p-guard-1',blob:new Blob([b],{type:'image/jpeg'}),meta:{id:'p-guard-1',lat:30,lon:31,t:Date.now()},room:ROOM});
     const ref0=SIG.refused||0;
     let resolved=false;
     acks.set('p-guard-1',{kind:'original',nonce:'n-guard',h:it.h,resolve:()=>{resolved=true},reject:()=>{}});
     const rt=Date.now(),wrongH='f'.repeat(64);
     const g1=await window._pcSign('BRAK1|p-guard-1|original|n-guard');
     const g2WrongH=await window._pcSign('BRAK2|p-guard-1|original|n-guard|'+wrongH+'|2|'+rt);
     await handleAck({t:'ack',id:'p-guard-1',kind:'original',nonce:'n-guard',status:200,c:2,h:wrongH,rt:rt,ip:g1.pub,is:g1.sig,is2:g2WrongH.sig});
     await handleAck({t:'ack',id:'p-guard-1',kind:'original',nonce:'n-guard',status:200,c:2,h:it.h,rt:rt,ip:g1.pub,is:g1.sig,is2:'bad_sig'});
     acks.delete('p-guard-1');

     /* Also test cgot with wrong h8 and bad BRCP1 signature */
     const recOff=await getItem('p-office-1');
     await copyPoll();
     const cn=COPY_REQ.nonce;
     const badCpSig=await window._pcSign('BRCP1|'+cn+'|'+rt+'|p-office-1:2:00000000');
     await handleCopyGot({t:'cgot',nonce:cn,rt:rt,items:[['p-office-1',2,'00000000']],ip:badCpSig.pub,is:badCpSig.sig});
     await copyPoll();
     const cn2=COPY_REQ.nonce;
     await handleCopyGot({t:'cgot',nonce:cn2,rt:rt,items:[['p-office-1',2,recOff.h.slice(0,8)]],ip:badCpSig.pub,is:'forged'});

     const stillGuard=await getItem('p-guard-1'),stillOff=await getItem('p-office-1');
     return {resolved,refDelta:(SIG.refused||0)-ref0,stillGuard:!!stillGuard,stillOff:!!stillOff&&stillOff.st===2};
    })()""")
    check(isinstance(bad_res, dict) and bad_res.get("resolved") is False
          and bad_res.get("refDelta") == 4
          and bad_res.get("stillGuard") is True and bad_res.get("stillOff") is True,
          f"wrong h/h8 or bad BRAK2/BRCP1 signature is refused and keeps the item ({bad_res})")

    # 5. cgot with c=2 and matching h8 transitions to st=3 ('✓✓') and deletes from IndexedDB q
    cgot_res = ajs(r"""(async()=>{
     const r1=await getItem('p-drive-1'),r2=await getItem('p-office-1');
     await copyPoll();
     const cn=COPY_REQ.nonce,rt=Date.now();
     const items=[['p-drive-1',2,r1.h.slice(0,8)],['p-office-1',2,r2.h.slice(0,8)]];
     const cs=items.map(x=>x[0]+':'+x[1]+':'+x[2]).join(',');
     const g=await window._pcSign('BRCP1|'+cn+'|'+rt+'|'+cs);
     await handleCopyGot({t:'cgot',nonce:cn,rt:rt,items:items,ip:g.pub,is:g.sig});
     const after1=await getItem('p-drive-1'),after2=await getItem('p-office-1');
     const b1=document.querySelector('[data-id="p-drive-1"] .badge').textContent;
     const b2=document.querySelector('[data-id="p-office-1"] .badge').textContent;
     return {after1:!!after1,after2:!!after2,b1,b2};
    })()""")
    check(isinstance(cgot_res, dict) and cgot_res.get("after1") is False and cgot_res.get("after2") is False
          and cgot_res.get("b1") == "✓✓" and cgot_res.get("b2") == "✓✓",
          f"cgot with c=2 and matching h8 deletes items from IndexedDB and marks badges '✓✓' ({cgot_res})")

    # 6. A 403 sets st=9 and keeps the item on the phone (never deleted, not re-sent, reason kept)
    ref_res = ajs(r"""(async()=>{
     await del('p-guard-1');
     while(busy)await new Promise(r=>setTimeout(r,20));
     const b=new Uint8Array([0xff,0xd8,0xff,0xe0,21,22,23,24,0xff,0xd9]);
     const it=await put({id:'p-refused-1',blob:new Blob([b],{type:'image/jpeg'}),meta:{id:'p-refused-1',lat:30,lon:31,t:Date.now()},room:ROOM});
     thumb(it,badgeFor(it));
     dc={readyState:'open',bufferedAmount:0,send:s=>{
      if(typeof s!=='string')return;
      const m=JSON.parse(s);
      if(m.t==='photo')window._lastPhotoMsg=m;
      else if(m.t==='end'){
       (async()=>{
        const pm=window._lastPhotoMsg;
        const g1=await window._pcSign('BRAK1|'+pm.id+'|'+pm.kind+'|'+pm.nonce);
        await handleAck({t:'ack',id:pm.id,kind:pm.kind,nonce:pm.nonce,status:403,reason:'Surveyor QR code revoked',ip:g1.pub,is:g1.sig});
       })();
      }
     }};
     await flush();
     const rec=await getItem('p-refused-1');
     const bdg=document.querySelector('[data-id="p-refused-1"] .badge');
     return {exists:!!rec,st:rec&&rec.st,why:rec&&rec.why,hasBlob:!!(rec&&rec.blob&&rec.blob.size>0),
      badge:bdg&&bdg.textContent,title:bdg&&bdg.title,pill:$('waitPill').textContent};
    })()""")
    check(isinstance(ref_res, dict) and ref_res.get("exists") is True
          and ref_res.get("st") == 9 and ref_res.get("why") == "Surveyor QR code revoked"
          and ref_res.get("hasBlob") is True and ref_res.get("badge") == "✗"
          and "refused" in (ref_res.get("pill") or "").lower(),
          f"a 403 sets st=9, keeps the sealed blob and refusal reason, and never deletes ({ref_res})")

    # 7. End-of-day sheet appears when pending items (st < 3 or st === 9) exist
    eod_res = ajs(r"""(async()=>{
     await checkEod(true);
     const shown=!$('eodSheet').classList.contains('hide');
     const listTxt=$('eodList').textContent;
     $('eodBtn').click();
     const hiddenAfterClick=$('eodSheet').classList.contains('hide');
     return {shown,listTxt,hiddenAfterClick};
    })()""")
    check(isinstance(eod_res, dict) and eod_res.get("shown") is True
          and "refused" in (eod_res.get("listTxt") or "").lower()
          and eod_res.get("hiddenAfterClick") is True,
          f"end-of-day sheet displays pending/refused items and dismisses cleanly ({eod_res})")

finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
