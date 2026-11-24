"""Google Drive GPS mailbox tests: PC PosIntake + phone LADDER_JS drive channel.

Run:  engine\\bin\\python-qgis.bat tests\\test_gps_drive_mailbox.py
"""
import hashlib
import json
import os
import re as _re
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PA, PB = free_port(), free_port()
PARENT, HELPER = f"http://127.0.0.1:{PA}", f"http://localhost:{PB}"
os.environ.update({
    "BIRUNI_DRIVE_API": HELPER,
    "BIRUNI_DRIVE_HELPER": HELPER,
    "BIRUNI_DRIVE_PARENT": PARENT,
    "BIRUNI_DRIVE_GSI": HELPER + "/gsi.js",
    "BIRUNI_DRIVE_PICKER": HELPER + "/picker.js",
    "BIRUNI_BRIDGE_RELAYS": f"ws://127.0.0.1:{free_port()}",
    "BIRUNI_BRIDGE_NTFY": "https://127.0.0.1:9/",
    "BIRUNI_BRIDGE_MQTT": "",
    "BIRUNI_BRIDGE_TUNNEL": "",
    "BIRUNI_BRIDGE_OWN": "",
    "QT_QPA_PLATFORM": "offscreen",
    "QTWEBENGINE_DISABLE_SANDBOX": "1",
    "QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu --no-sandbox",
})

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

from PyQt6.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings  # noqa: E402

app = QGuiApplication(sys.argv[:1])

from biruni.field_photos import bridge, drive, drive_page  # noqa: E402
from biruni.field_photos.ladder import Ladder  # noqa: E402
from biruni.field_photos.server import PhotoServer, all_phone_texts, phone_texts  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def wait(cond, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


# ---- 1. PC side: PosIntake -----------------------------------------------------------------------
work = tempfile.mkdtemp(prefix="biruni-gps-drv-")
texts = {"en": phone_texts(lambda s: s)}
srv = PhotoServer(os.path.join(work, "photos"), os.path.join(work, "cert"), texts,
                  "en", (), free_port(), ["127.0.0.1"], bind="127.0.0.1")
key = bridge.new_key()
srv.set_ladder(key)

root_dir = os.path.join(work, "drive-root")
os.makedirs(root_dir, exist_ok=True)
office_dir, created = drive.ensure_office_folder([root_dir], key)
check(created and os.path.isdir(office_dir), f"ensure_office_folder created {os.path.basename(office_dir)}")

g_dev1 = os.path.join(office_dir, "g-dev1")
p_dev1 = os.path.join(office_dir, "p-dev1")
os.makedirs(g_dev1, exist_ok=True)
os.makedirs(p_dev1, exist_ok=True)

# Untouched .bfp in p-dev1
bfp_path = os.path.join(p_dev1, "000001-0123456789ab.bfp")
with open(bfp_path, "wb") as f:
    f.write(b"BFP1\x00\x02{}")

lad_sender = Ladder(key)
now_ms = int(time.time() * 1000)


def write_bgp(folder, seq_start, points, lad_obj=lad_sender, suffix="abc123", truncate_to=None):
    seq_end = seq_start + len(points) - 1
    env = {
        "t": "lpos", "v": 1, "d": "device_drive_1", "e": "epochA",
        "lo": seq_start, "bl": len(points),
        "b": [[seq_start + i, pt] for i, pt in enumerate(points)],
        "n": f"nonce{seq_start}", "ts": now_ms,
    }
    sealed = lad_obj.seal(env)
    blob = b"BGP1\n" + sealed.encode("ascii")
    if truncate_to is not None:
        blob = blob[:truncate_to]
    name = f"{seq_start:09d}-{seq_end:09d}-{suffix}.bgp"
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(blob)
    return path, sealed


# Write two files out of numeric order in creation, plus a wrong-key file
pts1 = [{"id": "device_drive_1", "name": "D1", "lat": 24.701, "lon": 46.601, "acc": 4, "t": now_ms - 5000},
        {"id": "device_drive_1", "name": "D1", "lat": 24.702, "lon": 46.602, "acc": 4, "t": now_ms - 4000}]
pts2 = [{"id": "device_drive_1", "name": "D1", "lat": 24.703, "lon": 46.603, "acc": 4, "t": now_ms - 3000},
        {"id": "device_drive_1", "name": "D1", "lat": 24.704, "lon": 46.604, "acc": 4, "t": now_ms - 2000}]
_, sealed2 = write_bgp(g_dev1, 3, pts2)
_, sealed1 = write_bgp(g_dev1, 1, pts1)
wrong_lad = Ladder(bridge.new_key())
write_bgp(g_dev1, 90, [{"id": "device_drive_1", "name": "D1", "lat": 24.790, "lon": 46.690, "acc": 4, "t": now_ms}],
          lad_obj=wrong_lad, suffix="wrongk")

intake = drive.PosIntake([root_dir], key, srv.take_env, interval=10)
opened = intake.scan()
got = []
while not srv.events.empty():
    k_ev, v_ev = srv.events.get_nowait()
    if k_ev == "pos":
        got.append(v_ev)

check(opened == 2 and [p["seq"] for p in got] == [1, 2, 3, 4] and all(p.get("via") == "drive" for p in got),
      f"PosIntake delivers each seq once in order and ignores wrong key ({[p['seq'] for p in got]})")
check(os.path.isfile(bfp_path), "p-* folders and .bfp files are untouched")

# Same seqs arriving by another route are ignored
srv.take_env(sealed1, "mqtt")
dup_pos = [v for k_ev, v in iter(lambda: srv.events.get_nowait() if not srv.events.empty() else None, None)
           if k_ev == "pos"]
check(len(dup_pos) == 0, "same seqs arriving by another route (mqtt) are ignored")

# Truncated file is not remembered, then taken once whole
pts3 = [{"id": "device_drive_1", "name": "D1", "lat": 24.705, "lon": 46.605, "acc": 4, "t": now_ms - 1000}]
trunc_path, _ = write_bgp(g_dev1, 5, pts3, truncate_to=20)
check(intake.scan() == 0, "truncated .bgp file is not taken")
while not srv.events.empty():
    srv.events.get_nowait()
write_bgp(g_dev1, 5, pts3)
check(intake.scan() == 1, "truncated .bgp file was not remembered and is taken once whole")
whole_pos = []
while not srv.events.empty():
    k_ev, v_ev = srv.events.get_nowait()
    if k_ev == "pos":
        whole_pos.append(v_ev)
check([p["seq"] for p in whole_pos] == [5], f"seq 5 delivered once whole ({[p['seq'] for p in whole_pos]})")


# ---- 2. Phone side using fake Drive --------------------------------------------------------------
FAKE_GSI = r"""
window.google={accounts:{oauth2:{
 initTokenClient(c){return {requestAccessToken(o){window.__asked=(window.__asked||0)+1;
  setTimeout(()=>c.callback({access_token:'tok-'+window.__asked,expires_in:3600,scope:c.scope}),10)}}},
 hasGrantedAllScopes(r,s){return r.scope===s}}}};
(function poll(){const b=document.getElementById('go');
 if(b&&!b.disabled&&!window.__tapped){window.__tapped=1;b.click();setTimeout(poll,300)}
 else setTimeout(poll,50)})();
"""

FAKE_PICKER = r"""
window.gapi={load(n,o){setTimeout(()=>o.callback(),5)}};
window.google=window.google||{};
google.picker={ViewId:{FOLDERS:'folders'},DocsViewMode:{LIST:'list'},Response:{ACTION:'action',DOCUMENTS:'docs'},
 Action:{PICKED:'picked',CANCEL:'cancel'},Document:{ID:'id',NAME:'name',MIME_TYPE:'mimeType'},
 DocsView:class{constructor(v){this.o={view:v}}setSelectFolderEnabled(){return this}setIncludeFolders(){return this}
  setMimeTypes(m){this.o.mime=m;return this}setMode(){return this}setQuery(q){this.o.q=q;return this}
  setOwnedByMe(b){this.o.own=b;return this}},
 PickerBuilder:class{constructor(){this.v=[]}addView(v){this.v.push(v.o);return this}setOAuthToken(t){this.t=t;return this}
  setAppId(a){this.a=a;return this}setCallback(c){this.c=c;return this}
  build(){const s=this;return {setVisible(){fetch('/fake/pick?'+new URLSearchParams({t:s.t,a:s.a,q:s.v[0].q}))
   .then(r=>r.json()).then(d=>s.c(d))}}}}};
"""

PHONE = bridge.with_csp(bridge.phone_page(all_phone_texts()),
                        connect=(drive_page.API,), frames=(drive_page.HELPER,))

DRIVE_DISK = os.path.join(work, "fake-drive-disk")
os.makedirs(DRIVE_DISK, exist_ok=True)
FILES, FAKE = {}, {"next": 0, "pick": ["office"], "uploads": 0, "binned": []}
GRANTS = set()
PHONE_KEY = bridge.new_key()
OFFICE_NAME = drive.folder_name(PHONE_KEY)


def drive_path(fid):
    parts = []
    while fid in FILES:
        parts.insert(0, FILES[fid]["name"])
        fid = (FILES[fid]["parents"] or [None])[0]
    return os.path.join(DRIVE_DISK, *parts)


def drive_new(meta, data=None):
    FAKE["next"] += 1
    fid = f"fakeid_{FAKE['next']:04d}"
    FILES[fid] = {
        "id": fid, "name": meta["name"], "mimeType": meta.get("mimeType") or "application/octet-stream",
        "parents": list(meta.get("parents") or []), "appProperties": dict(meta.get("appProperties") or {}),
        "data": data, "app": meta.get("app", True),
        "createdTime": f"2026-11-18T00:{FAKE['next'] // 60:02d}:{FAKE['next'] % 60:02d}Z",
    }
    path = drive_path(fid)
    if data is None:
        os.makedirs(path, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return FILES[fid]


OFFICE_ID = drive_new({"name": OFFICE_NAME, "mimeType": "application/vnd.google-apps.folder", "app": False})["id"]


def visible(fid):
    return fid in FILES and (FILES[fid].get("app", True) or fid in GRANTS)


def drive_query(q):
    out = []
    for f in FILES.values():
        if f.get("trashed"):
            continue
        ok = True
        for m in _re.finditer(r"\b(name|mimeType)='((?:[^'\\]|\\.)*)'", q):
            if f[m.group(1)] != m.group(2):
                ok = False
        for m in _re.finditer(r"'([^']*)' in parents", q):
            ok = ok and m.group(1) in f["parents"]
        for m in _re.finditer(r"appProperties has \{ key='([^']*)' and value='([^']*)' \}", q):
            ok = ok and f["appProperties"].get(m.group(1)) == m.group(2)
        if ok and visible(f["id"]):
            out.append(f)
    return sorted(out, key=lambda f: f["createdTime"])


class FakeDriveHandler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", cors=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE")
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if not (self.headers.get("Authorization") or "").startswith("Bearer tok-"):
            self._send(401, b"{}", "application/json", cors=True)
            return False
        return True

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, cors=True)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/link/":
            self._send(200, drive_page.helper_page().encode("utf-8"))
        elif path == "/gsi.js":
            self._send(200, FAKE_GSI.encode("utf-8"), "text/javascript")
        elif path == "/picker.js":
            self._send(200, FAKE_PICKER.encode("utf-8"), "text/javascript")
        elif path == "/p/":
            self._send(200, PHONE.encode("utf-8"))
        elif path == "/fake/pick":
            how = FAKE["pick"].pop(0) if FAKE["pick"] else "cancel"
            if how == "office":
                GRANTS.add(OFFICE_ID)
                d = {"action": "picked", "docs": [{"id": OFFICE_ID, "name": OFFICE_NAME,
                                                    "mimeType": "application/vnd.google-apps.folder"}]}
            else:
                d = {"action": "cancel"}
            self._send(200, json.dumps(d).encode(), "application/json", cors=True)
        elif path == "/drive/v3/about":
            self._send(200, json.dumps({"user": {"emailAddress": "surveyor@example.com"}}).encode(),
                       "application/json", cors=True)
        elif path.startswith("/drive/v3/files/"):
            if not self._auth():
                return
            fid = path.rsplit("/", 1)[1]
            if not visible(fid):
                self._send(404, b"{}", "application/json", cors=True)
                return
            f = FILES[fid]
            self._send(200, json.dumps({
                "id": fid, "name": f["name"], "mimeType": f["mimeType"],
                "trashed": bool(f.get("trashed")), "ownedByMe": True,
                "capabilities": {"canAddChildren": True},
            }).encode(), "application/json", cors=True)
        elif path == "/drive/v3/files":
            if not self._auth():
                return
            from urllib.parse import parse_qs, urlsplit

            q = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
            files = [{"id": f["id"], "name": f["name"], "size": str(len(f["data"] or b"")),
                      "createdTime": f["createdTime"]} for f in drive_query(q)]
            self._send(200, json.dumps({"files": files}).encode(), "application/json", cors=True)
        else:
            self._send(404)

    def do_PATCH(self):  # noqa: N802
        if not self._auth():
            return
        fid = self.path.split("?", 1)[0].rsplit("/", 1)[1]
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not visible(fid):
            self._send(404, b"{}", "application/json", cors=True)
            return
        if body.get("trashed") is True:
            FILES[fid]["trashed"] = True
            FAKE["binned"].append(fid)
            p = drive_path(fid)
            if os.path.isfile(p):
                os.remove(p)
        self._send(200, json.dumps({"id": fid}).encode(), "application/json", cors=True)

    def do_POST(self):  # noqa: N802
        if not self._auth():
            return
        path = self.path.split("?", 1)[0]
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if path == "/drive/v3/files":
            f = drive_new(json.loads(body))
            self._send(200, json.dumps({"id": f["id"]}).encode(), "application/json", cors=True)
        elif path == "/upload/drive/v3/files":
            b = self.headers.get("Content-Type", "").split("boundary=", 1)[1].encode()
            parts = body.split(b"--" + b)
            meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0])
            data = parts[2].split(b"\r\n\r\n", 1)[1][:-2]
            f = drive_new(meta, data)
            FAKE["uploads"] += 1
            self._send(200, json.dumps({
                "id": f["id"], "size": str(len(data)),
                "sha256Checksum": hashlib.sha256(data).hexdigest(),
            }).encode(), "application/json", cors=True)
        else:
            self._send(404)


http_srvs = []
for port in (PA, PB):
    hs = ThreadingHTTPServer(("127.0.0.1", port), FakeDriveHandler)
    http_srvs.append(hs)
    threading.Thread(target=hs.serve_forever, daemon=True).start()

CSP_REFUSED = []
POPUPS = []


class DrivePhonePage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        print("phone js:", message, line, flush=True)
        if "Content Security Policy" in message:
            CSP_REFUSED.append(message)

    def createWindow(self, _type):  # noqa: N802
        w = DrivePhonePage(self.profile())
        POPUPS.append(w)
        w.windowCloseRequested.connect(lambda: (POPUPS.remove(w) if w in POPUPS else None, w.deleteLater()))
        return w


prof = QWebEngineProfile(app)
prof.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
ph = DrivePhonePage(prof)
ph.load(QUrl(f"{PARENT}/p/#k={PHONE_KEY}&i={bridge.pin(PHONE_KEY)}&l=en&m=gps&g=1"))

res = {}


def js(expr):
    res.pop("v", None)
    ph.runJavaScript(expr, lambda v: res.update(v=v))
    wait(lambda: "v" in res, 10)
    return res.get("v")


check(wait(lambda: js("typeof DRV==='object' && DRV.ok() && !!document.querySelector('.dpick:not(.hide)')") is True, 20),
      "phone linked Google Drive and shows folder picker button")
ph.runJavaScript("document.querySelector('.dpick').click()")
check(wait(lambda: js("DRV.go()") is True, 20),
      "phone picked office folder and created g-<dev> subfolder")

devid = js("DEVID")
check(js("!!(DRV.fold && DRV.fold.g && !DRV.fold.p)") is True
      and any(f["name"] == f"g-{devid}" for f in FILES.values())
      and not any(f["name"] == f"p-{devid}" for f in FILES.values()),
      f"only g-{devid} folder created (no p-{devid})")

dstate_text = js("DRV.card.querySelector('.dstate').textContent") or ""
check("up to a minute late" in dstate_text, f"driveSay('idle') shows driveGps text ({dstate_text!r})")

# Put a dummy sealed photo in DB and verify dfits() is false and no .bfp is uploaded
js(r"""(async()=>{
 await put({id:'ph1',blob:new Blob(['fakejpeg']),meta:{id:'ph1',sealed:1,t:Date.now()},room:ROOM});
 await drivePump();
})();1""")
check(js("dfits({meta:{sealed:1},blob:new Blob(['x'])})") is False, "dfits() is false when !DRIVE_ON")

# Push 3 GPS positions with every channel dead
js(r"""(() => {
 $('me').value = 'SurveyorG';
 const now = Date.now();
 (async () => {
  for (let i = 0; i < 3; i++) {
   fix = {lat: 24.750 + i * 0.001, lon: 46.650, acc: 4, t: now - (2 - i) * 1000};
   await tellPos(true);
  }
 })();
})();1""")

bgp_files = lambda: [f for f in FILES.values() if f["name"].endswith(".bgp") and not f.get("trashed")]
bfp_files = lambda: [f for f in FILES.values() if f["name"].endswith(".bfp")]

check(wait(lambda: len(bgp_files()) == 1, 25),
      f"with g=1 and every channel dead, one .bgp appears in g-{devid} ({[f['name'] for f in bgp_files()]})")
check(len(bfp_files()) == 0, "no photo (.bfp) is uploaded")

# Verify PC PosIntake can read the phone-generated .bgp file
srv2 = PhotoServer(os.path.join(work, "photos2"), os.path.join(work, "cert"), texts,
                   "en", (), free_port(), ["127.0.0.1"], bind="127.0.0.1")
srv2.set_ladder(PHONE_KEY)
intake2 = drive.PosIntake([DRIVE_DISK], PHONE_KEY, srv2.take_env, interval=10)
check(intake2.scan() == 1, "PC PosIntake reads phone-written .bgp file")
phone_pos = []
while not srv2.events.empty():
    k_ev, v_ev = srv2.events.get_nowait()
    if k_ev == "pos":
        phone_pos.append(v_ev)
check(len(phone_pos) == 3 and [p["seq"] for p in phone_pos] == [1, 2, 3],
      f"all 3 positions decoded with seqs [1, 2, 3] ({[p['seq'] for p in phone_pos]})")

# Deliver signed ack to phone and verify .bgp goes to the bin on cleanup tick
ack_sealed = srv2.ladder.seal({
    "t": "lack", "d": devid, "e": js("LAD.epoch"), "upto": 3,
    "n": "ack123456", "ts": int(time.time() * 1000),
    **dict(zip(("ip", "is"), srv2.ladder.sign(f"BRLA1|{devid}|{js('LAD.epoch')}|3"))),
})
js(f"SIG.arrived({json.dumps(ack_sealed)}, 'mqtt'); 1")
check(wait(lambda: js("LAD.upto") == 3 and js("LAD.backlog") == 0, 10),
      "phone accepts signed ack and clears backlog")

js("LAD._lastDriveClean = 0; 1")
check(wait(lambda: len(FAKE["binned"]) == 1 and len(bgp_files()) == 0, 10),
      f"after ack, .bgp file moves to the bin (binned={FAKE['binned']})")

# Unacked .bgp file is NEVER trashed, even if older than 6 hours (P1 invariant)
js(r"""(() => {
 const now = Date.now();
 (async () => {
  fix = {lat: 24.755, lon: 46.655, acc: 4, t: now};
  await tellPos(true);
 })();
})();1""")
check(wait(lambda: len(bgp_files()) == 1, 25),
      f"second .bgp uploaded with seq 4 ({[f['name'] for f in bgp_files()]})")
# Backdate the #g record's timestamp by 8 hours (> 6 hours) while LAD.upto is still 3 (< hi=4)
js(r"""(async () => {
 const recs = await dgall();
 for (const r of recs) {
  r.at = Date.now() - 8 * 3600 * 1000;
  await dput(r);
 }
 LAD._lastDriveClean = 0;
 await LAD._driveClean();
 window._unackedCleanDone = true;
})();1""")
check(wait(lambda: js("window._unackedCleanDone") is True, 10), "ran _driveClean on 8-hour-old unacked .bgp")
check(len(FAKE["binned"]) == 1 and len(bgp_files()) == 1,
      f"a .bgp that was never acked is NOT trashed even after 8 hours (binned={FAKE['binned']}, live={[f['name'] for f in bgp_files()]})")

check(not CSP_REFUSED, f"no CSP violations logged ({CSP_REFUSED})")

srv.stop()
srv2.stop()
for hs in http_srvs:
    hs.shutdown()
shutil.rmtree(work, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
