r"""No-loss P6 stage C: phone page cloud mailbox on Cloudflare R2 (QWebEngine + local `tests\mb_node_server.mjs`).

Tests:
- descriptor_gate
- pc_off_reaches_st1
- pc_opens_c_rises
- phone_deletes_at_2
- bad_receipt_kept
- two_mailboxes_no_delete
- office_then_r2
- relay_without_mailbox
- no_csp_violation

Run:  engine\bin\python-qgis.bat tests\test_mailbox.py
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


NODE = shutil.which("node") or next(
    (p for p in (r"C:\Program Files\nodejs\node.exe", r"C:\Program Files (x86)\nodejs\node.exe") if os.path.isfile(p)),
    "node",
)
SERVER_MJS = os.path.join(HERE, "mb_node_server.mjs")
MB_SECRET = secrets.token_urlsafe(32)


def start_worker_node(secret=MB_SECRET):
    env = dict(os.environ)
    if secret:
        env["MB_SECRET"] = secret
    else:
        env.pop("MB_SECRET", None)
    proc = subprocess.Popen(
        [NODE, SERVER_MJS],
        cwd=HERE,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    port = None
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        if line.startswith("PORT "):
            port = int(line.split()[1])
            break
    if not port:
        err_out = proc.stderr.read() if proc.stderr else ""
        proc.kill()
        raise RuntimeError(f"mb_node_server.mjs did not print PORT ({err_out})")
    return proc, f"http://127.0.0.1:{port}"


node_proc, MB_BASE = start_worker_node(MB_SECRET)
node_nomb_proc, MB_NOMB_BASE = start_worker_node(secret="")

PAGE_PORT = free_port()
PAGE_ORIGIN = f"http://127.0.0.1:{PAGE_PORT}"
HELPER_ORIGIN = f"http://localhost:{PAGE_PORT}"

os.environ.update({
    "BIRUNI_MB_BASE": MB_BASE,
    "BIRUNI_DRIVE_API": HELPER_ORIGIN,
    "BIRUNI_DRIVE_HELPER": HELPER_ORIGIN,
    "BIRUNI_DRIVE_PARENT": PAGE_ORIGIN,
    "BIRUNI_DRIVE_GSI": HELPER_ORIGIN + "/gsi.js",
    "BIRUNI_DRIVE_PICKER": HELPER_ORIGIN + "/picker.js",
    "BIRUNI_BRIDGE_RELAYS": f"ws://127.0.0.1:{free_port()}",
    "BIRUNI_BRIDGE_NTFY": "https://127.0.0.1:9/",
    "BIRUNI_BRIDGE_MQTT": "",
    "BIRUNI_BRIDGE_TUNNEL": "",
    "BIRUNI_BRIDGE_OWN": "",
    "BIRUNI_BRIDGE_ORG": "",
    "QT_QPA_PLATFORM": "offscreen",
    "QT_QPA_FONTDIR": r"C:\Windows\Fonts",
    "QTWEBENGINE_DISABLE_SANDBOX": "1",
    "QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu --no-sandbox",
})

from PyQt6.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402

app = QGuiApplication(sys.argv[:1])

from biruni.field_photos import bridge, drive, drive_page, mailbox, mailbox_page, recipients  # noqa: E402
from biruni.field_photos.handover import HandoverKey  # noqa: E402
from biruni.field_photos.server import PhotoStore, all_phone_texts  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def wait(cond, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


def http_call(base, method, path, token=None, body=None, content_type=None):
    headers = {"User-Agent": "Biruni-Test"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if content_type:
        headers["Content-Type"] = content_type
    if body is not None:
        headers["Content-Length"] = str(len(body))
    req = urllib.request.Request(base + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def r2_keys(base=MB_BASE):
    st, raw = http_call(base, "GET", "/__keys")
    if st != 200:
        return []
    return json.loads(raw.decode("utf-8")).get("keys") or []


class FakeSettings:
    def __init__(self):
        self.d = {}

    def value(self, k, default=None):
        return self.d.get(k, default)

    def setValue(self, k, v):
        self.d[k] = v


PHONE_HTML = bridge.with_csp(
    bridge.phone_page(all_phone_texts()),
    connect=(drive_page.API, MB_BASE, MB_NOMB_BASE),
    frames=(drive_page.HELPER,),
)


class PageHandler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/p/", "/p/index.html"):
            body = PHONE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/p/sw.js":
            body = bridge.SW_JS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/link/":
            body = drive_page.helper_page().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/gsi.js", "/picker.js"):
            body = b""
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()


http_srv = ThreadingHTTPServer(("127.0.0.1", PAGE_PORT), PageHandler)
threading.Thread(target=http_srv.serve_forever, daemon=True).start()

CONSOLE = []
CSP_ERRORS = []


class MbPhonePage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        CONSOLE.append(message)
        print("phone js:", message, line, flush=True)
        if "Content Security Policy" in message:
            CSP_ERRORS.append(message)


work = tempfile.mkdtemp(prefix="biruni_p6_mb_phone_")
try:
    st_store = FakeSettings()
    k = bridge.load_key(st_store)
    id_sk, pin = bridge.identity(k)
    tag = drive.room_tag(k)
    host = "biruni-relay.myorg.workers.dev"
    ut, ot = mailbox.tokens(MB_SECRET, tag)

    cert_dir = os.path.join(work, "cert")
    photos_dir = os.path.join(work, "photos")
    mirror_dir = os.path.join(work, "mirror")
    os.makedirs(cert_dir, exist_ok=True)
    os.makedirs(photos_dir, exist_ok=True)
    os.makedirs(mirror_dir, exist_ok=True)
    hk = HandoverKey(cert_dir)

    profile = QWebEngineProfile(app)
    ph = MbPhonePage(profile)
    loaded = []
    ph.loadFinished.connect(loaded.append)
    ph.load(QUrl(f"{PAGE_ORIGIN}/p/#k={k}&i={pin}&l=en&d=1&pk={hk.public}"))

    res_box = {}

    def js(expr):
        res_box.pop("v", None)
        ph.runJavaScript(expr, lambda v: res_box.update(v=v))
        wait(lambda: "v" in res_box, 10)
        return res_box.get("v")

    def ajs(expr):
        ph.runJavaScript(
            "window.__r=undefined;(async()=>{try{window.__r=JSON.stringify(await (async()=>{\n%s\n})())}"
            "catch(e){window.__r=JSON.stringify('error '+e)}})()" % ("return (" + expr + ")" if "return " not in expr else expr)
        )
        wait(lambda: js("window.__r!==undefined") is True, 15)
        v = js("window.__r")
        return json.loads(v) if isinstance(v, str) else None

    check(wait(lambda: bool(loaded), 15)
          and wait(lambda: js("typeof BK!=='undefined' && !!BK && !!db && ID.pin==='%s' && typeof mbTake==='function'" % pin) is True, 15),
          "phone page booted with bridgeKey, IndexedDB, ID.pin, and mailbox_page.py loaded")

    # Configure NL=true as an office with no-loss support does
    ajs("hello({t:'hello',nl:1,now:Date.now(),eod:18,kinds:['Pole'],form:[],pk:PCKEY}); return true;")
    devid = js("DEVID")

    # ---- 1. descriptor_gate ----------------------------------------------------------------------------------------
    desc_valid = mailbox.descriptor(k, host, MB_SECRET, 14)
    v_valid = json.loads(desc_valid["l"])

    # (a) Tampered payload with original signature -> refused
    desc_tampered = dict(desc_valid, l=desc_valid["l"].replace('"days":14', '"days":7'))
    ok_tampered = ajs(f"mbTake({json.dumps(desc_tampered)})")

    # (b) Signed by a different identity key -> refused
    other_k = bridge.new_key()
    other_sk, _ = bridge.identity(other_k)
    other_ip, other_sig = recipients.sign_with(other_sk, desc_valid["l"], prefix="BRMB1|")
    ok_wrong_signer = ajs(f"mbTake({json.dumps({'l': desc_valid['l'], 'ip': other_ip, 'is': other_sig})})")

    # (c) Missing ID.pin -> refused even when signature is valid (IDOK returns true without pin!)
    ok_no_pin = ajs(f"""
     const savedPin=ID.pin;ID.pin=null;
     try{{return await mbTake({json.dumps(desc_valid)})}}
     finally{{ID.pin=savedPin}}
    """)

    # (d) Wrong room / wrong tag / bad host / bad ut -> refused
    def make_signed_desc(**overrides):
        p = dict(v_valid, **overrides)
        l_txt = recipients.canonical(p)
        ip, sig = recipients.sign_with(id_sk, l_txt, prefix="BRMB1|")
        return {"l": l_txt, "ip": ip, "is": sig}

    ok_wrong_room = ajs(f"mbTake({json.dumps(make_signed_desc(room='000000000000'))})")
    ok_wrong_tag = ajs(f"mbTake({json.dumps(make_signed_desc(tag='0' * 16))})")
    ok_bad_host = ajs(f"mbTake({json.dumps(make_signed_desc(h='evil.example.com'))})")
    ok_bad_ut = ajs(f"mbTake({json.dumps(make_signed_desc(ut='short'))})")

    # (e) Valid descriptor -> accepted and persisted in biruni-mb-<ROOM>
    ok_valid = ajs(f"mbTake({json.dumps(desc_valid)})")

    # (f) Higher seq accepted, then lower seq rollback -> refused
    desc_higher_seq = make_signed_desc(seq=v_valid["seq"] + 100, days=30)
    ok_higher = ajs(f"mbTake({json.dumps(desc_higher_seq)})")
    ok_rollback = ajs(f"mbTake({json.dumps(make_signed_desc(seq=v_valid['seq'] + 50, days=7))})")

    kept_mb = ajs("JSON.parse(kept('biruni-mb-'+ROOM)||'null')")
    check(ok_tampered is False
          and ok_wrong_signer is False
          and ok_no_pin is False
          and ok_wrong_room is False
          and ok_wrong_tag is False
          and ok_bad_host is False
          and ok_bad_ut is False
          and ok_valid is True
          and ok_higher is True
          and ok_rollback is False
          and isinstance(kept_mb, dict) and kept_mb.get("d", {}).get("seq") == v_valid["seq"] + 100,
          f"descriptor_gate (tampered={ok_tampered}, wrong_signer={ok_wrong_signer}, no_pin={ok_no_pin}, "
          f"wrong_room={ok_wrong_room}, wrong_tag={ok_wrong_tag}, bad_host={ok_bad_host}, bad_ut={ok_bad_ut}, "
          f"valid={ok_valid}, higher={ok_higher}, rollback={ok_rollback})")

    # ---- 2. pc_off_reaches_st1 -------------------------------------------------------------------------------------
    # With PC offline (lineOpen()===false), phone captures a photo -> mbPump uploads .bfp to R2 -> item reaches st=1, c=1, r2=1
    st1_res = ajs(r"""
     const jpg=new Uint8Array([0xff,0xd8,0xff,0xe0,0,16,74,70,73,70,0,1,1,0,0,1,0,1,0,0,11,22,33,44,0xff,0xd9]);
     const it=await put({id:'mb-photo-0001',blob:new Blob([jpg],{type:'image/jpeg'}),
      meta:{id:'mb-photo-0001',lat:30.0444,lon:31.2357,acc:4,t:Date.now(),surveyor:'Hatem'},room:ROOM});
     thumb(it,badgeFor(it));
     await mbPump();
     const rec=await getItem('mb-photo-0001');
     const mrec=await dget('#mmb-photo-0001');
     const dAll=await dall(),dgAll=await dgall();
     const bdg=document.querySelector('[data-id="mb-photo-0001"] .badge');
     return {
      exists:!!rec,st:rec&&rec.st,c:rec&&rec.c,r2:rec&&rec.r2,h:rec&&rec.h,
      mrecSt:mrec&&mrec.st,mrecName:mrec&&mrec.name,
      inDall:dAll.some(x=>x.id==='#mmb-photo-0001'),inDgall:dgAll.some(x=>x.id==='#mmb-photo-0001'),
      badge:bdg&&bdg.textContent,pill:$('waitPill').textContent
     };
    """)
    keys_after_1 = [k_obj for k_obj in r2_keys() if k_obj.startswith(f"o/{tag}/{devid}/")]
    check(isinstance(st1_res, dict)
          and st1_res.get("exists") is True
          and st1_res.get("st") == 1
          and st1_res.get("c") == 1
          and st1_res.get("r2") == 1
          and st1_res.get("mrecSt") == "done"
          and st1_res.get("inDall") is False
          and st1_res.get("inDgall") is False
          and st1_res.get("badge") == "☁️"
          and len(keys_after_1) == 1,
          f"pc_off_reaches_st1 (st1_res={st1_res}, keys={keys_after_1})")

    # ---- 3. pc_opens_c_rises ---------------------------------------------------------------------------------------
    rc_c1 = mailbox.sign_receipt(k, devid, [["mb-photo-0001", 1, st1_res["h"][:8]]])
    rc_c1_bytes = json.dumps(rc_c1, separators=(",", ":")).encode("utf-8")
    put_rc1_st, _ = http_call(MB_BASE, "PUT", f"/mb/rc/{tag}/{devid}.json", token=ot, body=rc_c1_bytes, content_type="application/json")
    c1_res = ajs(r"""
     await mbReceipts();
     const rec=await getItem('mb-photo-0001');
     const bdg=document.querySelector('[data-id="mb-photo-0001"] .badge');
     return {exists:!!rec,st:rec&&rec.st,c:rec&&rec.c,badge:bdg&&bdg.textContent};
    """)
    check(put_rc1_st == 200 and isinstance(c1_res, dict)
          and c1_res.get("exists") is True and c1_res.get("st") == 2 and c1_res.get("c") == 1
          and c1_res.get("badge") == "✓",
          f"pc_opens_c_rises: receipt with c=1 advances st=1 -> st=2 ('✓') without deleting ({c1_res})")

    # Now run real PC MailboxIntake.scan() which pulls the .bfp from R2, saves to PhotoStore + mirror, and writes receipt with c>=2
    store = PhotoStore(photos_dir, mirror_folder=mirror_dir)
    events = queue.Queue()
    intake = mailbox.MailboxIntake(host, MB_SECRET, store, hk, k, events, cert_dir, days=14)
    scan_out = intake.scan()
    c_pc, _h_pc, h8_pc = store.ledger.receipt_info("mb-photo-0001")
    check(scan_out["new"] == 1 and "mb-photo-0001" in store.records and c_pc >= 2 and h8_pc == st1_res["h"][:8],
          f"pc_opens_c_rises: PC MailboxIntake pulled mb-photo-0001 and wrote receipt with c={c_pc} ({scan_out})")

    # ---- 4. phone_deletes_at_2 -------------------------------------------------------------------------------------
    del_res = ajs(r"""
     const skewBefore=SKEW;
     await mbReceipts();
     const rec=await getItem('mb-photo-0001');
     const bdg=document.querySelector('[data-id="mb-photo-0001"] .badge');
     return {exists:!!rec,badge:bdg&&bdg.textContent,skewUnchanged:SKEW===skewBefore};
    """)
    check(isinstance(del_res, dict)
          and del_res.get("exists") is False
          and del_res.get("badge") == "✓✓"
          and del_res.get("skewUnchanged") is True,
          f"phone_deletes_at_2: signed receipt with c>=2 deletes item from phone IDB and sets badge '✓✓' ({del_res})")

    # ---- 5. bad_receipt_kept ---------------------------------------------------------------------------------------
    bad_rc_setup = ajs(r"""
     const jpg=new Uint8Array([0xff,0xd8,0xff,0xe0,0,16,74,70,73,70,0,1,1,0,0,1,0,1,0,0,55,66,77,88,0xff,0xd9]);
     const it=await put({id:'mb-photo-0002',blob:new Blob([jpg],{type:'image/jpeg'}),
      meta:{id:'mb-photo-0002',lat:30.05,lon:31.24,acc:4,t:Date.now(),surveyor:'Hatem'},room:ROOM});
     thumb(it,badgeFor(it));
     await mbPump();
     const rec=await getItem('mb-photo-0002');
     return {st:rec&&rec.st,h:rec&&rec.h};
    """)
    h2 = bad_rc_setup["h"]

    # (a) Receipt with wrong h8 (signed by PC, but h8 does not match it.h) -> refused, item kept
    rc_bad_h8 = mailbox.sign_receipt(k, devid, [["mb-photo-0002", 2, "00000000"]])
    http_call(MB_BASE, "PUT", f"/mb/rc/{tag}/{devid}.json", token=ot,
              body=json.dumps(rc_bad_h8).encode("utf-8"), content_type="application/json")
    res_bad_h8 = ajs(r"""
     const ref0=SIG.refused||0;
     await mbReceipts();
     const rec=await getItem('mb-photo-0002');
     return {exists:!!rec,st:rec&&rec.st,refDelta:(SIG.refused||0)-ref0};
    """)

    # (b) Receipt with forged signature -> refused, item kept
    rc_forged = mailbox.sign_receipt(k, devid, [["mb-photo-0002", 2, h2[:8]]])
    rc_forged["is"] = ("A" if rc_forged["is"][0] != "A" else "B") + rc_forged["is"][1:]
    http_call(MB_BASE, "PUT", f"/mb/rc/{tag}/{devid}.json", token=ot,
              body=json.dumps(rc_forged).encode("utf-8"), content_type="application/json")
    res_forged = ajs(r"""
     const ref0=SIG.refused||0;
     await mbReceipts();
     const rec=await getItem('mb-photo-0002');
     return {exists:!!rec,st:rec&&rec.st,refDelta:(SIG.refused||0)-ref0};
    """)

    # (c) Receipt with wrong nonce (e.g. 'mb.other_dev') -> ignored, item kept
    rc_wrong_nonce = mailbox.sign_receipt(k, "other_dev", [["mb-photo-0002", 2, h2[:8]]])
    http_call(MB_BASE, "PUT", f"/mb/rc/{tag}/{devid}.json", token=ot,
              body=json.dumps(rc_wrong_nonce).encode("utf-8"), content_type="application/json")
    res_wrong_nonce = ajs(r"""
     await mbReceipts();
     const rec=await getItem('mb-photo-0002');
     return {exists:!!rec,st:rec&&rec.st};
    """)

    check(res_bad_h8.get("exists") is True and res_bad_h8.get("refDelta") == 1
          and res_forged.get("exists") is True and res_forged.get("refDelta") == 1
          and res_wrong_nonce.get("exists") is True,
          f"bad_receipt_kept (bad_h8={res_bad_h8}, forged={res_forged}, wrong_nonce={res_wrong_nonce})")

    # Now let PC pull mb-photo-0002 and write valid receipt so phone cleans it up
    intake.scan()
    ajs("mbReceipts()")
    check(ajs("getItem('mb-photo-0002')") is None, "mb-photo-0002 deleted after valid PC receipt")

    # ---- 6. two_mailboxes_no_delete --------------------------------------------------------------------------------
    two_mb_res = ajs(r"""
     const jpg=new Uint8Array([0xff,0xd8,0xff,0xe0,0,16,74,70,73,70,0,1,1,0,0,1,0,1,0,0,91,92,93,94,0xff,0xd9]);
     const it=await put({id:'mb-photo-both',blob:new Blob([jpg],{type:'image/jpeg'}),
      meta:{id:'mb-photo-both',lat:30.06,lon:31.25,acc:4,t:Date.now(),surveyor:'Hatem'},room:ROOM});
     thumb(it,badgeFor(it));
     /* Simulate Drive upload completing first: record in store d with n=42, item at st=1, c=1 */
     const tag=await BFP.tag(KEYB);
     await dput({id:'mb-photo-both',room:tag,n:42,name:'000042-aaaaaaaaaaaa.bfp',size:100,fsha:'aa',st:'done',fid:'fid-42',at:Date.now()});
     await setState('mb-photo-both',1,1);
     const fitsAtSt1=mbFits(await getItem('mb-photo-both'));
     await mbPump();
     const rec=await getItem('mb-photo-both');
     const mrec=await dget('#mmb-photo-both');
     const fitsAfterR2=mbFits(rec);
     return {
      fitsAtSt1,
      exists:!!rec,
      st:rec&&rec.st,
      c:rec&&rec.c,
      r2:rec&&rec.r2,
      reusedN:mrec&&mrec.n,
      fitsAfterR2
     };
    """)
    check(isinstance(two_mb_res, dict)
          and two_mb_res.get("fitsAtSt1") is True
          and two_mb_res.get("exists") is True
          and two_mb_res.get("st") == 1
          and two_mb_res.get("c") == 1
          and two_mb_res.get("r2") == 1
          and two_mb_res.get("reusedN") == 42
          and two_mb_res.get("fitsAfterR2") is False,
          f"two_mailboxes_no_delete: Drive + R2 stays at st=1, c=1, reuses Drive n=42, and never deletes ({two_mb_res})")

    # Clean up mb-photo-both via PC intake + receipt
    intake.scan()
    ajs("mbReceipts()")
    check(ajs("getItem('mb-photo-both')") is None, "mb-photo-both deleted once PC pulls it and writes c>=2 receipt")

    # ---- 7. office_then_r2 -----------------------------------------------------------------------------------------
    photos_nomirror = os.path.join(work, "photos_nomirror")
    os.makedirs(photos_nomirror, exist_ok=True)
    store_nomirror = PhotoStore(photos_nomirror)  # no mirror folder -> office alone has c=1

    off_r2_setup = ajs(r"""
     /* While lineOpen() is true, an item at st=0 is NOT taken by mbFits (line sends it directly) */
     dc={readyState:'open',bufferedAmount:0,send:()=>{}};
     const jpg=new Uint8Array([0xff,0xd8,0xff,0xe0,0,16,74,70,73,70,0,1,1,0,0,1,0,1,0,0,101,102,103,0xff,0xd9]);
     const it=await put({id:'mb-photo-off1',blob:new Blob([jpg],{type:'image/jpeg'}),
      meta:{id:'mb-photo-off1',lat:30.07,lon:31.26,acc:4,t:Date.now(),surveyor:'Hatem'},room:ROOM});
     thumb(it,badgeFor(it));
     const fitsWhileLineOpenAtSt0=mbFits(await getItem('mb-photo-off1'));
     dc=null;
     /* Office receives it over direct line with c=1 -> phone state becomes st=2, c=1 */
     await setState('mb-photo-off1',2,1);
     const fitsAtSt2=mbFits(await getItem('mb-photo-off1'));
     await mbPump();
     const recAfterR2=await getItem('mb-photo-off1');
     return {
      fitsWhileLineOpenAtSt0,
      fitsAtSt2,
      stillExistsAfterR2Upload:!!recAfterR2,
      stAfterR2Upload:recAfterR2&&recAfterR2.st,
      cAfterR2Upload:recAfterR2&&recAfterR2.c,
      r2Flag:recAfterR2&&recAfterR2.r2,
      h:recAfterR2&&recAfterR2.h
     };
    """)
    check(isinstance(off_r2_setup, dict)
          and off_r2_setup.get("fitsWhileLineOpenAtSt0") is False
          and off_r2_setup.get("fitsAtSt2") is True
          and off_r2_setup.get("stillExistsAfterR2Upload") is True
          and off_r2_setup.get("stAfterR2Upload") == 2
          and off_r2_setup.get("cAfterR2Upload") == 1
          and off_r2_setup.get("r2Flag") == 1,
          f"office_then_r2: phone uploads st=2 item to R2 and keeps it at st=2, c=1 until PC confirms ({off_r2_setup})")

    store_nomirror.save(
        {"id": "mb-photo-off1", "t": int(time.time() * 1000), "lat": 30.07, "lon": 31.26,
         "surveyor": "Hatem", "device_id": devid, "route": "direct"},
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\x65\x66\x67\xff\xd9",
        ssha=off_r2_setup["h"],
    )
    c_before_r2, _, _ = store_nomirror.ledger.receipt_info("mb-photo-off1")
    intake_nomirror = mailbox.MailboxIntake(
        host, MB_SECRET, store_nomirror, hk, k, queue.Queue(), os.path.join(work, "cert_nm"), days=14
    )
    intake_nomirror.scan()
    c_after_r2, _, _ = store_nomirror.ledger.receipt_info("mb-photo-off1")
    ajs("mbReceipts()")
    check(c_before_r2 == 1 and c_after_r2 == 2 and ajs("getItem('mb-photo-off1')") is None,
          f"office_then_r2: PC pull adds r2 place (c {c_before_r2}->{c_after_r2}) and receipt deletes item on phone")

    # ---- 8. relay_without_mailbox ----------------------------------------------------------------------------------
    nomb_res = ajs(f"""
     MBX.BASE={json.dumps(MB_NOMB_BASE)};
     MBX.until=0;MBX.fails=0;MBX.dead=false;
     const jpg=new Uint8Array([0xff,0xd8,0xff,0xe0,0,16,74,70,73,70,0,1,1,0,0,1,0,1,0,0,111,112,0xff,0xd9]);
     const it=await put({{id:'mb-photo-501',blob:new Blob([jpg],{{type:'image/jpeg'}}),
      meta:{{id:'mb-photo-501',lat:30.08,lon:31.27,acc:4,t:Date.now(),surveyor:'Hatem'}},room:ROOM}});
     const t0=Date.now();
     await mbPump();
     const rec=await getItem('mb-photo-501');
     const waitMs=MBX.until-t0;
     MBX.BASE={json.dumps(MB_BASE)};
     return {{st:rec&&rec.st,r2:rec&&rec.r2,waitMs}};
    """)
    check(isinstance(nomb_res, dict)
          and nomb_res.get("st") == 0
          and not nomb_res.get("r2")
          and 3500000 <= (nomb_res.get("waitMs") or 0) <= 3700000,
          f"relay_without_mailbox: 501 backs off 1 hour and keeps item at st=0 ({nomb_res})")

    # Also verify no DELETE appears in mailbox_page.py and no secret/token was logged to console
    mb_js_src = mailbox_page.MAILBOX_JS
    check("DELETE" not in mb_js_src, "no DELETE request exists in mailbox_page.py")
    check(all(secret_val not in "\n".join(CONSOLE) for secret_val in (MB_SECRET, ut, ot)),
          "no token or secret logged to browser console")

    # ---- 9. no_csp_violation ---------------------------------------------------------------------------------------
    check(not CSP_ERRORS, f"no_csp_violation: zero CSP console violations ({CSP_ERRORS})")

finally:
    http_srv.shutdown()
    node_proc.kill()
    node_nomb_proc.kill()
    shutil.rmtree(work, ignore_errors=True)

print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
