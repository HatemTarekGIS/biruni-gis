r"""No-loss P5: tracking modes, consent sheet/pill/record, hs sender, and interval queue in the real phone page (QWebEngine).

Tests:
- no_tellpos_when_off
- none_outside_hours
- five_minute_cadence
- photo_mode_only_on_shot
- unsigned_or_rolled_back_policy_refused
- pinless_link_ignores_policy
- nothing_before_consent
- widening_reasks_consent
- withdraw_stops_and_records
- gps_mode_exempt
- interval_points_queue_offline

Run:  engine\bin\python-qgis.bat tests\test_tracking_modes.py
"""
import datetime as dt
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

from biruni.field_photos import bridge, handover, policy, recipients, server  # noqa: E402

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


def load_phone_page(raw_html, url_str):
    pg = QWebEnginePage()
    pg.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    pg.javaScriptConsoleMessage = lambda lv, msg, ln, src: print("  js:", msg, ln, flush=True)
    ready = []
    pg.loadFinished.connect(ready.append)
    pg.setHtml(raw_html, QUrl(url_str))
    wait(lambda: bool(ready), 15)

    def js(code):
        box = []
        pg.runJavaScript(code, box.append)
        wait(lambda: bool(box), 10)
        return box[0] if box else None

    def ajs(expr):
        pg.runJavaScript(
            "window.__r=undefined;(async()=>{try{window.__r=JSON.stringify(await (%s))}"
            "catch(e){window.__r=JSON.stringify('error '+e)}})()" % expr
        )
        wait(lambda: js("window.__r!==undefined") is True, 15)
        v = js("window.__r")
        return json.loads(v) if isinstance(v, str) else None

    return pg, js, ajs


print("=== P5 tracking modes & consent (QWebEngine) ===", flush=True)
work = tempfile.mkdtemp(prefix="biruni_p5_modes_")
try:
    for n in ("RELAYS", "LEGACY_RELAYS", "MQTT", "LEGACY_MQTT"):
        setattr(bridge, n, ())
    bridge.NTFY = ""
    bridge.OWN = ""

    key = bridge.new_key()
    sk_b64, pin = bridge.identity(key)
    room = key[:12]
    cert_dir = os.path.join(work, "cert")
    backup_dir = os.path.join(work, "backup")
    office = handover.HandoverKey(cert_dir)
    backup = handover.HandoverKey(backup_dir)
    recipients.add(cert_dir, backup.public, "backup", "Branch", office_pub=office.public)
    rl = recipients.signed(cert_dir, office.public, key)

    raw_page = bridge.phone_page(server.all_phone_texts())

    # ---- Main surveyor page (pinned link) ---------------------------------------------------------------------------
    page, js, ajs = load_phone_page(
        raw_page, f"https://photos.biruni-gis.com/p/#k={key}&i={pin}&l=en&d=1&pk={office.public}"
    )
    check(wait(lambda: js("!!BK && !!db && ID.pin==='%s'" % pin) is True, 10),
          "pinned surveyor page initialized")

    # Set up a fake fix and intercept sendPos / sendHs
    ajs(f"""(async()=>{{
     $('me').value='Hatem';keep('biruni-me','Hatem');
     await rlTake({json.dumps(rl)});
     window._sentPos=[];window._sentHs=[];
     sendPos=async(p,force)=>{{window._sentPos.push(Object.assign({{_force:!!force}},p))}};
     sendHs=async(h)=>{{window._sentHs.push(h)}};
     fix={{lat:30.0444,lon:31.2357,acc:5,alt:20,heading:90,speed:1.2,t:NOW()}};
     return true}})()""")

    def sign_pl(seq, mode, hours=(), tz=180, per=None, jobs=None, signer_sk=sk_b64, rm=room):
        lst = {
            "v": 1,
            "seq": seq,
            "room": rm,
            "made": int(time.time()),
            "mode": mode,
            "hours": list(hours),
            "tz": tz,
            "per": dict(per or {}),
            "jobs": dict(jobs or {}),
        }
        l_text = policy.canonical(lst)
        ip, sig = recipients.sign_with(signer_sk, l_text, prefix=policy.SIGN_PREFIX)
        return {"l": l_text, "ip": ip, "is": sig}

    # 1. unsigned_or_rolled_back_policy_refused
    other_sk, _ = bridge.identity(bridge.new_key())
    bad_res = ajs(f"""(async()=>{{
     const rUnsigned=await plTake({{l:JSON.stringify({{v:1,seq:10,room:ROOM,mode:'live',hours:[],tz:0,per:{{}},jobs:{{}}}}),ip:'',is:''}});
     const rWrongSigner=await plTake({json.dumps(sign_pl(10, 'live', signer_sk=other_sk))});
     const rWrongRoom=await plTake({json.dumps(sign_pl(10, 'live', rm='wrongroom123'))});
     const rOk10=await plTake({json.dumps(sign_pl(10, '5'))});
     const rRollback=await plTake({json.dumps(sign_pl(9, 'live'))});
     return {{rUnsigned,rWrongSigner,rWrongRoom,rOk10,rRollback,seq:plGet().lst.seq}};
    }})()""")
    check(bad_res == {
        "rUnsigned": False,
        "rWrongSigner": False,
        "rWrongRoom": False,
        "rOk10": True,
        "rRollback": False,
        "seq": 10,
    }, f"unsigned_or_rolled_back_policy_refused ({bad_res})")

    # 2. nothing_before_consent
    before_cons = ajs(r"""(async()=>{
     window._sentPos=[];
     const sheetOpen=!$('consSheet').classList.contains('hide');
     const shareHidden=$('shareRow').classList.contains('hide');
     const modeBefore=plMode();
     await tellPos(true);
     await tellPos(true,'shot');
     return {sheetOpen,shareHidden,modeBefore,sentCount:window._sentPos.length,
             bodyTxt:$('consBody').textContent,limitTxt:$('consLimit').textContent};
    })()""")
    check(isinstance(before_cons, dict)
          and before_cons.get("sheetOpen") is True
          and before_cons.get("shareHidden") is True
          and before_cons.get("modeBefore") == "off"
          and before_cons.get("sentCount") == 0
          and "coordinates" in before_cons.get("bodyTxt", "")
          and "locked" in before_cons.get("limitTxt", ""),
          f"nothing_before_consent ({before_cons})")

    # Accept consent for seq=10, mode='5' -> check BCN1 record sealed for rlFor('state') (v2 for office+backup)
    cons_item = ajs(r"""(async()=>{
     const it=await recordConsent(true);
     const buf=new Uint8Array(await it.blob.arrayBuffer());
     return {id:it.id,cn:it.cn,cap:it.cap,vByte:buf[0],b64:b64u.enc(buf),modeAfter:plMode(),pillTxt:$('trkPill').textContent};
    })()""")
    import base64
    raw_sealed_cn = base64.urlsafe_b64decode(cons_item["b64"] + "===")
    m_off, d_off = office.open(raw_sealed_cn)
    m_bak, d_bak = backup.open(raw_sealed_cn)
    bcn_json = json.loads(d_off[4:].decode("utf-8"))
    check(cons_item["vByte"] == 2 and d_off[:4] == b"BCN1" and d_off == d_bak
          and m_off.get("cn") == 1 and bcn_json["seq"] == 10 and bcn_json["mode"] == "5" and bcn_json["a"] is True
          and cons_item["modeAfter"] == "5" and "5" in cons_item["pillTxt"],
          f"consent record is BCN1 sealed for rlFor('state') (office + backup) ({bcn_json})")

    # 3. five_minute_cadence
    cad_res = ajs(r"""(async()=>{
     window._sentPos=[];
     lastPos=null;
     const t0=1764000000000;
     noteTime(t0);
     fix={lat:30.0444,lon:31.2357,acc:4,t:NOW()};
     await tellPos(true);
     const n1=window._sentPos.length;
     /* 2 minutes later: must not send even with force=true */
     noteTime(t0+2*60000);
     fix={lat:30.0450,lon:31.2360,acc:4,t:NOW()};
     await tellPos(true);
     const n2=window._sentPos.length;
     /* 5 minutes later: sends */
     noteTime(t0+5*60000);
     fix={lat:30.0460,lon:31.2370,acc:4,t:NOW()};
     await tellPos(false);
     const n3=window._sentPos.length;
     return {n1,n2,n3,tm:window._sentPos[0]&&window._sentPos[0].tm};
    })()""")
    check(cad_res == {"n1": 1, "n2": 1, "n3": 2, "tm": "5"},
          f"five_minute_cadence ({cad_res})")

    # 4. photo_mode_only_on_shot (narrowing from '5' to 'photo' does not re-ask consent!)
    photo_res = ajs(f"""(async()=>{{
     window._sentPos=[];lastPos=null;
     await plTake({json.dumps(sign_pl(11, 'photo'))});
     const sheetOpen=!$('consSheet').classList.contains('hide');
     const modeNow=plMode();
     await tellPos(true);
     const afterBeat=window._sentPos.length;
     await tellPos(true,'shot');
     const afterShot=window._sentPos.length;
     return {{sheetOpen,modeNow,afterBeat,afterShot,tm:window._sentPos[0]&&window._sentPos[0].tm}};
    }})()""")
    check(photo_res == {"sheetOpen": False, "modeNow": "photo", "afterBeat": 0, "afterShot": 1, "tm": "photo"},
          f"photo_mode_only_on_shot ({photo_res})")

    # 5. widening_reasks_consent (rising from consented '5' to '1' or changing hours re-asks consent and blocks until accepted)
    widen_res = ajs(f"""(async()=>{{
     window._sentPos=[];lastPos=null;
     await plTake({json.dumps(sign_pl(12, '1'))});
     const sheetOnWiden=!$('consSheet').classList.contains('hide');
     const modeWhilePending=plMode();
     await tellPos(true);
     const sentWhilePending=window._sentPos.length;
     await recordConsent(true);
     const modeAfterAccept=plMode();
     /* Now keep mode '1' but change hours -> must re-ask consent */
     await plTake({json.dumps(sign_pl(13, '1', hours=[[0, 480, 1020]], tz=180))});
     const sheetOnHours=!$('consSheet').classList.contains('hide');
     const modeWhileHoursPending=plMode();
     await recordConsent(true);
     return {{sheetOnWiden,modeWhilePending,sentWhilePending,modeAfterAccept,sheetOnHours,modeWhileHoursPending}};
    }})()""")
    check(widen_res == {
        "sheetOnWiden": True,
        "modeWhilePending": "off",
        "sentWhilePending": 0,
        "modeAfterAccept": "1",
        "sheetOnHours": True,
        "modeWhileHoursPending": "off",
    }, f"widening_reasks_consent ({widen_res})")

    # 6. none_outside_hours
    # Sunday 2026-11-22 07:30 UTC -> 10:30 local (UTC+3), inside [0, 480, 1020]
    sun_in_ms = int(dt.datetime(2026, 11, 22, 7, 30, tzinfo=dt.timezone.utc).timestamp() * 1000)
    # Sunday 2026-11-22 16:00 UTC -> 19:00 local (UTC+3), outside [0, 480, 1020]
    sun_out_ms = int(dt.datetime(2026, 11, 22, 16, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    hrs_res = ajs(f"""(async()=>{{
     window._sentPos=[];lastPos=null;
     noteTime({sun_out_ms});
     const modeOutside=plMode();
     await tellPos(true);
     const sentOutside=window._sentPos.length;
     noteTime({sun_in_ms});
     const modeInside=plMode();
     await tellPos(true);
     const sentInside=window._sentPos.length;
     return {{modeOutside,sentOutside,modeInside,sentInside}};
    }})()""")
    check(hrs_res == {"modeOutside": "off", "sentOutside": 0, "modeInside": "1", "sentInside": 1},
          f"none_outside_hours ({hrs_res})")

    # 7. withdraw_stops_and_records (tapping trkPill reopens sheet with Withdraw; clicking Withdraw stops tracking and queues BCN1 a:false)
    withdraw_res = ajs(r"""(async()=>{
     window._sentPos=[];lastPos=null;
     $('trkPill').click();
     const sheetOpen=!$('consSheet').classList.contains('hide');
     const noBtnTxt=$('consNo').textContent;
     const it=await recordConsent(false);
     const buf=new Uint8Array(await it.blob.arrayBuffer());
     const modeAfter=plMode();
     await tellPos(true);
     return {sheetOpen,noBtnTxt,modeAfter,sentAfter:window._sentPos.length,b64:b64u.enc(buf)};
    })()""")
    raw_w = base64.urlsafe_b64decode(withdraw_res["b64"] + "===")
    _, d_w = office.open(raw_w)
    w_json = json.loads(d_w[4:].decode("utf-8"))
    check(withdraw_res["sheetOpen"] is True
          and "Withdraw" in withdraw_res["noBtnTxt"]
          and withdraw_res["modeAfter"] == "off"
          and withdraw_res["sentAfter"] == 0
          and w_json["a"] is False and w_json["seq"] == 13,
          f"withdraw_stops_and_records ({withdraw_res}, bcn={w_json})")

    # 8. no_tellpos_when_off (and hs beat still works when hs:1 was seen!)
    off_res = ajs(f"""(async()=>{{
     window._sentPos=[];window._sentHs=[];lastPos=null;
     await plTake({json.dumps(sign_pl(14, 'off'))});
     await tellPos(true);
     await tellPos(true,'shot');
     const hsBeforeCap=await reportHs();
     hello({{t:'hello',hs:1,nl:1,now:{sun_in_ms}}});
     const hsAfterCap=await reportHs();
     return {{mode:plMode(),posSent:window._sentPos.length,hsBeforeCap,hsSent:window._sentHs.length,lastHs:window._sentHs[0]||null}};
    }})()""")
    check(off_res["mode"] == "off" and off_res["posSent"] == 0
          and off_res["hsBeforeCap"] is None
          and off_res["hsSent"] >= 1 and off_res["lastHs"]["tm"] == "off",
          f"no_tellpos_when_off and hs sender gated by hs:1 ({off_res})")

    # 9. interval_points_queue_offline (in 5-min mode, LAD.push keeps and persists multiple points instead of overwriting _items=[item])
    iv_res = ajs(f"""(async()=>{{
     await plTake({json.dumps(sign_pl(15, '5'))});
     await recordConsent(true);
     LAD.cap=true;
     LAD._items=[];
     await LAD.push({{id:DEVID,lat:30.01,lon:31.01,t:NOW()-600000,tm:'5'}});
     await LAD.push({{id:DEVID,lat:30.02,lon:31.02,t:NOW()-300000,tm:'5'}});
     await LAD.push({{id:DEVID,lat:30.03,lon:31.03,t:NOW(),tm:'5'}});
     const r=IDBKeyRange.bound(ROOM+':',ROOM+':\uffff');
     const inDb=LAD.db?(await LAD._tx('readonly',s=>s.getAll(r))||[]).length:0;
     return {{memCount:LAD._items.length,inDb}};
    }})()""")
    check(iv_res["memCount"] == 3 and iv_res["inDb"] >= 3,
          f"interval_points_queue_offline ({iv_res})")
    page.deleteLater()

    # ---- 10. pinless_link_ignores_policy (no &i= in link -> takes no policy, keeps toggle) -------------------------
    key_nopin = bridge.new_key()
    sk_np, _ = bridge.identity(key_nopin)
    rm_np = key_nopin[:12]
    pg_np, js_np, ajs_np = load_phone_page(
        raw_page, f"https://photos.biruni-gis.com/p/#k={key_nopin}&l=en"
    )
    check(wait(lambda: js_np("!!BK && !!db && !ID.pin") is True, 10),
          "pinless page initialized without ID.pin")
    np_res = ajs_np(f"""(async()=>{{
     const taken=await plTake({json.dumps(sign_pl(1, 'off', signer_sk=sk_np, rm=rm_np))});
     const shareVisible=!$('shareRow').classList.contains('hide');
     $('share').checked=true;
     const modeOn=plMode();
     $('share').checked=false;
     const modeOff=plMode();
     return {{taken,shareVisible,modeOn,modeOff}};
    }})()""")
    check(np_res == {"taken": False, "shareVisible": True, "modeOn": "live", "modeOff": "off"},
          f"pinless_link_ignores_policy ({np_res})")
    pg_np.deleteLater()

    # ---- 11. gps_mode_exempt (m=gps ignores policy and stays live) -------------------------------------------------
    key_gps = bridge.new_key()
    sk_gps, pin_gps = bridge.identity(key_gps)
    rm_gps = key_gps[:12]
    pg_gps, js_gps, ajs_gps = load_phone_page(
        raw_page, f"https://photos.biruni-gis.com/p/#k={key_gps}&i={pin_gps}&m=gps&l=en"
    )
    check(wait(lambda: js_gps("!!BK && GPSMODE===true") is True, 10),
          "m=gps page initialized")
    gps_res = ajs_gps(f"""(async()=>{{
     const taken=await plTake({json.dumps(sign_pl(1, 'off', signer_sk=sk_gps, rm=rm_gps))});
     return {{taken,mode:plMode(),sheetHidden:$('consSheet').classList.contains('hide')}};
    }})()""")
    check(gps_res == {"taken": False, "mode": "live", "sheetHidden": True},
          f"gps_mode_exempt ({gps_res})")
    pg_gps.deleteLater()

finally:
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
