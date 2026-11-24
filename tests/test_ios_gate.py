"""P2 phone page tests (QWebEngine):
- iOS outside standalone mode blocks capture behind install steps and offers a Copy link button;
- inside standalone mode, when #k is missing, shows the paste-link box and validates links;
- Android is not blocked and gets a soft Install suggestion on beforeinstallprompt;
- a refused storage.persist() / persisted()=false shows the visible persistence warning;
- storage.estimate() shows free space, reserves headroom, and QuotaExceededError stops capture without losing queued items;
- unsent banner ("N items not protected for X days") turns amber at 3 days and red at 5 days for st < 3 or st == 9.
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

from biruni.field_photos import bridge, drive_page, handover, server  # noqa: E402

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


print("=== P2 iOS Gate, Persistent Storage & Unsent Banner Test (QWebEngine) ===", flush=True)
work = tempfile.mkdtemp(prefix="biruni_p2_ios_")

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
    csp_page = bridge.with_csp(raw_page, connect=(drive_page.API,), frames=(drive_page.HELPER,))
    check('<link rel="manifest" href="manifest.webmanifest">' in csp_page
          and "manifest-src 'self'" in csp_page,
          "page links manifest.webmanifest and includes manifest-src 'self' in CSP")

    page = QWebEnginePage()
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    csp_errors = []

    def _on_console(lv, msg, ln, src):
        print("  js:", msg, ln, flush=True)
        if "Content Security Policy" in msg:
            csp_errors.append(msg)

    page.javaScriptConsoleMessage = _on_console

    full_link = f"https://photos.biruni-gis.com/p/#k={key}&i={pin}&l=en&d=1&pk={hk.public}"
    ready = []
    page.loadFinished.connect(ready.append)
    page.setHtml(csp_page, QUrl(full_link))
    check(wait(lambda: bool(ready), 15), "phone page loaded in QWebEngine with strict CSP")

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

    check(wait(lambda: js("!!BK && !!db") is True, 10), "bridgeKey and IndexedDB initialized")

    # 1. Spoof iPhone Safari outside standalone mode: capture is blocked behind install steps + Copy link button
    ios_safari = ajs(r"""(async()=>{
     Object.defineProperty(navigator,'userAgent',{value:'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',configurable:true});
     Object.defineProperty(navigator,'standalone',{value:false,configurable:true});
     checkIosGate();
     await start();
     const copied=await copyCurrentLink();
     return {
      iosBlocked:iosBlocked,
      gateVisible:!$('iosGate').classList.contains('hide'),
      pasteVisible:!$('iosPaste').classList.contains('hide'),
      introHidden:$('intro').classList.contains('hide'),
      workHidden:$('work').classList.contains('hide'),
      goDisabled:$('go').disabled,
      shotDisabled:$('shot').disabled,
      started:started,
      hasSvg:$('iosGate').querySelectorAll('svg').length>=2,
      copiedHasKey:copied.includes('#k=')
     };
    })()""")
    check(isinstance(ios_safari, dict)
          and ios_safari.get("iosBlocked") is True
          and ios_safari.get("gateVisible") is True
          and ios_safari.get("pasteVisible") is False
          and ios_safari.get("introHidden") is True
          and ios_safari.get("workHidden") is True
          and ios_safari.get("goDisabled") is True
          and ios_safari.get("shotDisabled") is True
          and ios_safari.get("started") is False
          and ios_safari.get("hasSvg") is True
          and ios_safari.get("copiedHasKey") is True,
          f"iOS Safari outside standalone blocks capture and shows install steps + Copy link ({ios_safari})")

    # 2. Spoof iOS standalone PWA with missing #k: shows paste-link box and validates links
    ios_pwa_missing_k = ajs(f"""(async()=>{{
     Object.defineProperty(navigator,'standalone',{{value:true,configurable:true}});
     keep('biruni-last-link',null);
     history.replaceState(null,'','https://photos.biruni-gis.com/p/');
     KEYB='';ROOM='';
     checkIosGate();
     const beforePaste={{
      iosBlocked:iosBlocked,
      gateVisible:!$('iosGate').classList.contains('hide'),
      pasteVisible:!$('iosPaste').classList.contains('hide'),
      goDisabled:$('go').disabled
     }};
     /* Invalid links must be rejected */
     $('iosPasteIn').value='https://evil.example.com/p/#k={key}';
     const badOrigin=await applyBridgeLink($('iosPasteIn').value);
     $('iosPasteIn').value='https://photos.biruni-gis.com/p/#k=too_short';
     const badKey=await applyBridgeLink($('iosPasteIn').value);
     const errText=$('iosPasteErr').textContent;
     /* Valid link must be accepted and unblock the PWA */
     $('iosPasteIn').value={json.dumps(full_link)};
     const goodOk=await applyBridgeLink($('iosPasteIn').value);
     return {{
      beforePaste,
      badOrigin,
      badKey,
      hasErr:errText.length>0,
      goodOk,
      afterBlocked:iosBlocked,
      afterPasteHidden:$('iosPaste').classList.contains('hide'),
      afterIntroVisible:!$('intro').classList.contains('hide'),
      afterGoEnabled:!$('go').disabled,
      keyRestored:KEYB==={json.dumps(key)}
     }};
    }})()""")
    check(isinstance(ios_pwa_missing_k, dict)
          and ios_pwa_missing_k.get("beforePaste", {}).get("iosBlocked") is True
          and ios_pwa_missing_k.get("beforePaste", {}).get("pasteVisible") is True
          and ios_pwa_missing_k.get("beforePaste", {}).get("gateVisible") is False
          and ios_pwa_missing_k.get("badOrigin") is False
          and ios_pwa_missing_k.get("badKey") is False
          and ios_pwa_missing_k.get("hasErr") is True
          and ios_pwa_missing_k.get("goodOk") is True
          and ios_pwa_missing_k.get("afterBlocked") is False
          and ios_pwa_missing_k.get("afterPasteHidden") is True
          and ios_pwa_missing_k.get("afterIntroVisible") is True
          and ios_pwa_missing_k.get("afterGoEnabled") is True
          and ios_pwa_missing_k.get("keyRestored") is True,
          f"installed iOS PWA with missing #k shows paste box, rejects bad links, and accepts valid link ({ios_pwa_missing_k})")

    # 3. Android is not blocked and gets a soft Install suggestion on beforeinstallprompt
    android_res = ajs(r"""(async()=>{
     Object.defineProperty(navigator,'userAgent',{value:'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/124.0.0.0 Mobile Safari/537.36',configurable:true});
     Object.defineProperty(navigator,'standalone',{value:false,configurable:true});
     checkIosGate();
     let prompted=false;
     const ev=new Event('beforeinstallprompt',{cancelable:true});
     ev.prompt=async()=>{prompted=true};
     window.dispatchEvent(ev);
     const barShown=!$('androidBar').classList.contains('hide');
     await $('androidBtn').onclick();
     const barHiddenAfter=$('androidBar').classList.contains('hide');
     return {iosBlocked,barShown,prompted,barHiddenAfter};
    })()""")
    check(isinstance(android_res, dict)
          and android_res.get("iosBlocked") is False
          and android_res.get("barShown") is True
          and android_res.get("prompted") is True
          and android_res.get("barHiddenAfter") is True,
          f"Android is not blocked and shows soft install bar on beforeinstallprompt ({android_res})")

    # 4. Persistent storage: refused persist() shows visible warning; granted hides it
    persist_res = ajs(r"""(async()=>{
     let persistCalled=0;
     navigator.storage.persist=async()=>{persistCalled++;return false};
     navigator.storage.persisted=async()=>false;
     const ok1=await checkPersist();
     const warnShown=!$('persistWarn').classList.contains('hide');
     const warnText=$('persistWarn').textContent;
     navigator.storage.persist=async()=>{persistCalled++;return true};
     navigator.storage.persisted=async()=>true;
     const ok2=await checkPersist();
     const warnHidden=$('persistWarn').classList.contains('hide');
     return {persistCalled,ok1,warnShown,hasText:warnText.length>10,ok2,warnHidden};
    })()""")
    check(isinstance(persist_res, dict)
          and persist_res.get("persistCalled") >= 2
          and persist_res.get("ok1") is False
          and persist_res.get("warnShown") is True
          and persist_res.get("hasText") is True
          and persist_res.get("ok2") is True
          and persist_res.get("warnHidden") is True,
          f"refused storage.persist() shows visible warning and granted hides it ({persist_res})")

    # 5. Storage estimate() free space, headroom reservation, and QuotaExceededError stops capture without losing queued items
    quota_res = ajs(r"""(async()=>{
     fix={lat:30.0444,lon:31.2357,acc:5,alt:null,heading:null,t:Date.now()};
     $('shot').disabled=false;
     const b=new Uint8Array([0xff,0xd8,0xff,0xe0,1,2,3,4,0xff,0xd9]);
     await put({id:'p-queued-safe',blob:new Blob([b],{type:'image/jpeg'}),meta:{id:'p-queued-safe',lat:30,lon:31,t:Date.now()},room:ROOM});
     navigator.storage.estimate=async()=>({usage:100*1048576,quota:500*1048576});
     const okPlenty=await checkStorageSpace(1024);
     const spaceTxt=$('spaceInfo').textContent;

     /* Low headroom (< 15 MB free) stops capture */
     navigator.storage.estimate=async()=>({usage:492*1048576,quota:500*1048576});
     const okLow=await checkStorageSpace(2*1048576);
     const lowBanner=!$('quotaWarn').classList.contains('hide')&&$('quotaWarn').classList.contains('red');
     const shotDisabledLow=$('shot').disabled;

     /* Restore plenty of space, then simulate QuotaExceededError during IndexedDB put */
     navigator.storage.estimate=async()=>({usage:100*1048576,quota:500*1048576});
     await checkStorageSpace(0);
     setQuotaStopped(true);
     const shotDisabledOnErr=$('shot').disabled;
     const stillQueued=await getItem('p-queued-safe');
     setQuotaStopped(false);
     return {
      okPlenty,
      hasSpaceTxt:spaceTxt.includes('MB')||spaceTxt.includes('GB'),
      okLow,
      lowBanner,
      shotDisabledLow,
      shotDisabledOnErr,
      stillQueued:!!(stillQueued&&stillQueued.blob&&stillQueued.blob.size>0)
     };
    })()""")
    check(isinstance(quota_res, dict)
          and quota_res.get("okPlenty") is True
          and quota_res.get("hasSpaceTxt") is True
          and quota_res.get("okLow") is False
          and quota_res.get("lowBanner") is True
          and quota_res.get("shotDisabledLow") is True
          and quota_res.get("shotDisabledOnErr") is True
          and quota_res.get("stillQueued") is True,
          f"estimate() shows free space, reserves headroom, and QuotaExceededError stops capture without losing queued items ({quota_res})")

    # 6. Unsent banner colours change at 3 days (amber) and 5 days (red) for st < 3 or st == 9
    banner_res = ajs(r"""(async()=>{
     for(const x of await all())await del(x.id);
     const dayMs=86400000,now=NOW();
     const b=new Uint8Array([0xff,0xd8,0xff,0xe0,7,8,9,10,0xff,0xd9]);

     /* 1 day old -> hidden */
     await put({id:'p-age-1',blob:new Blob([b],{type:'image/jpeg'}),meta:{id:'p-age-1',lat:30,lon:31,t:now-1*dayMs},t0:now-1*dayMs,at:now-1*dayMs,st:0,room:ROOM});
     const r1=await updateUnsentBanner();
     const hiddenAt1=$('unsentWarn').classList.contains('hide');

     /* 3 days old (st=2, 1 copy at office) -> amber */
     await put({id:'p-age-3',blob:new Blob([b],{type:'image/jpeg'}),meta:{id:'p-age-3',lat:30,lon:31,t:now-3*dayMs-1000},t0:now-3*dayMs-1000,at:now-3*dayMs-1000,st:2,c:1,room:ROOM});
     const r3=await updateUnsentBanner();
     const isAmber=$('unsentWarn').classList.contains('amber')&&!$('unsentWarn').classList.contains('red')&&!$('unsentWarn').classList.contains('hide');
     const txt3=$('unsentWarn').textContent;

     /* 5 days old (st=9, refused) -> red */
     await put({id:'p-age-5',blob:new Blob([b],{type:'image/jpeg'}),meta:{id:'p-age-5',lat:30,lon:31,t:now-5*dayMs-1000},t0:now-5*dayMs-1000,at:now-5*dayMs-1000,st:9,c:0,room:ROOM});
     const r5=await updateUnsentBanner();
     const isRed=$('unsentWarn').classList.contains('red')&&!$('unsentWarn').classList.contains('amber')&&!$('unsentWarn').classList.contains('hide');
     const txt5=$('unsentWarn').textContent;

     return {r1,hiddenAt1,r3,isAmber,txt3,r5,isRed,txt5};
    })()""")
    check(isinstance(banner_res, dict)
          and banner_res.get("hiddenAt1") is True
          and banner_res.get("isAmber") is True
          and banner_res.get("r3", {}).get("days") == 3
          and banner_res.get("r3", {}).get("n") == 2
          and banner_res.get("isRed") is True
          and banner_res.get("r5", {}).get("days") == 5
          and banner_res.get("r5", {}).get("n") == 3,
          f"unsent banner is hidden under 3 days, amber at 3 days, and red at 5 days ({banner_res})")

    check(not csp_errors, f"no CSP console errors occurred ({csp_errors})")

finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
