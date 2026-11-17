r"""Field photos through Google Drive: the link page (field_photos/drive_page.py, served at drive.biruni-gis.com/link/).

Runs the page in Chromium (QtWebEngine) with a fake Google sign-in script and a fake Drive on this PC, on three
origins: the photo page's (allowed), the link page's own, and a stranger's. Checked: opened alone it only explains;
framed by the photo page it hands over a token for the nonce it was given, under its CSP, then gives the cached token
after a reload without a new sign-in; a hello with a bad nonce gets nothing; a stranger framing it gets nothing at all.

Run:  engine\bin\python-qgis.bat tests\test_field_photos_drive_link.py
"""
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PA, PB, PC = free_port(), free_port(), free_port()
PARENT, HELPER, STRANGER = f"http://127.0.0.1:{PA}", f"http://localhost:{PB}", f"http://127.0.0.1:{PC}"
os.environ.update({"BIRUNI_DRIVE_API": HELPER, "BIRUNI_DRIVE_HELPER": HELPER, "BIRUNI_DRIVE_PARENT": PARENT,
                   "BIRUNI_DRIVE_GSI": HELPER + "/gsi.js", "BIRUNI_DRIVE_PICKER": HELPER + "/picker.js"})
os.environ["BIRUNI_BRIDGE_RELAYS"] = f"ws://127.0.0.1:{free_port()}"   # nothing there: never the public relays
os.environ["BIRUNI_BRIDGE_NTFY"] = "https://127.0.0.1:9/"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
sys.path.insert(0, os.path.join(HERE, "..", "app", "python"))

from PyQt6.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402

app = QGuiApplication(sys.argv[:1])
from biruni.field_photos import bridge, drive_page  # noqa: E402
from biruni.field_photos.server import all_phone_texts  # noqa: E402

PHONE = bridge.with_csp(bridge.phone_page(all_phone_texts()), connect=(drive_page.API,), frames=(drive_page.HELPER,))
LINK_GETS = []

failures, CONSOLE, ABOUT = [], [], []


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


FAKE_GSI = r"""
window.google={accounts:{oauth2:{
 initTokenClient(c){return {requestAccessToken(o){window.__asked=(window.__asked||0)+1;
  setTimeout(()=>c.callback({access_token:'tok-'+window.__asked,expires_in:3600,scope:c.scope}),10)}}},
 hasGrantedAllScopes(r,s){return r.scope===s}}}};
(function poll(){const b=document.getElementById('go');   /* a user's tap, once the page allows it */
 if(b&&!b.disabled&&!window.__tapped){window.__tapped=1;b.click();setTimeout(poll,300)}
 else if(window.__tapped&&b&&!b.disabled)fetch('/fake/tap?'+new URLSearchParams({l:b.textContent})).then(r=>r.text())
  .then(t=>{if(t==='1')b.click();setTimeout(poll,300)},()=>setTimeout(poll,300));
 else setTimeout(poll,50)})();
"""

FAKE_PICKER = r"""
window.gapi={load(n,o){window.__pkload=(window.__pkload||0)+1;setTimeout(()=>o.callback(),5)}};
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

PARENT_PAGE = r"""<!doctype html><html><body><script>
window.got=[];const bad=location.search.indexOf('bad')>=0,NONCE=bad?'x':'n'+Math.random().toString(36).slice(2,14);
const f=document.createElement('iframe');f.src='__HELPER__/link/';document.body.appendChild(f);
addEventListener('message',e=>{if(e.source!==f.contentWindow)return;got.push(e.data);
 if(e.data&&e.data.type==='biruni-drive-ready')
  f.contentWindow.postMessage({v:1,type:'biruni-drive-hello',nonce:NONCE,label:'Link Google Drive',dir:'ltr'},'__HELPER__')});
window.NONCE=NONCE;
</script></body></html>""".replace("__HELPER__", HELPER)


# ---- a fake Google Drive: folders and files in memory, each file also written where Drive for desktop would put it
import hashlib  # noqa: E402
import re as _re  # noqa: E402
import tempfile as _tf  # noqa: E402

DRIVE_DISK = _tf.mkdtemp()
FILES, FAKE = {}, {"drop": 0, "uploads": 0, "next": 0, "pick": [], "picks": [], "fault": [], "times": [],
                  "tap": 0, "tapped": [], "deleted": [], "binned": []}
GRANTS, PICKER_GETS = set(), []


def chain(fid):
    while fid in FILES:
        yield FILES[fid]
        fid = (FILES[fid]["parents"] or [None])[0]


def visible(fid):
    """drive.file: the app sees what it made and what the user picked, unless a folder above was taken back"""
    return (fid in FILES and (FILES[fid].get("app", True) or fid in GRANTS)
            and not any(f.get("revoked") for f in chain(fid)))


def writable(fid):
    return all(f.get("canAdd", True) for f in chain(fid))


def drive_error(handler, code, reason):
    handler._send(code, json.dumps({"error": {"code": code, "errors": [{"reason": reason}]}}).encode(),
                  "application/json", cors=True)


def parent_refused(handler, meta):
    """404 if the parent cannot be seen, 403 if it is shared for viewing only; else False"""
    par = (meta.get("parents") or [None])[0]
    if par is None:
        return False
    if not visible(par):
        drive_error(handler, 404, "notFound")
        return True
    if not writable(par):
        drive_error(handler, 403, "insufficientFilePermissions")
        return True
    return False


def drive_path(fid):
    parts = []
    while fid in FILES:
        parts.insert(0, FILES[fid]["name"])
        fid = (FILES[fid]["parents"] or [None])[0]
    return os.path.join(DRIVE_DISK, *parts)


def drive_new(meta, data=None):
    FAKE["next"] += 1
    fid = f"f{FAKE['next']}"
    FILES[fid] = {"id": fid, "name": meta["name"], "mimeType": meta.get("mimeType") or "application/octet-stream",
                  "parents": list(meta.get("parents") or []), "appProperties": dict(meta.get("appProperties") or {}),
                  "data": data, "createdTime": f"2026-11-17T00:{FAKE['next'] // 60:02d}:{FAKE['next'] % 60:02d}Z"}
    path = drive_path(fid)
    if data is None:
        os.makedirs(path, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return FILES[fid]


def drive_query(q):
    out = []
    for f in FILES.values():
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


class Handler(BaseHTTPRequestHandler):
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
            self.send_header("Access-Control-Expose-Headers", "Retry-After")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, cors=True)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/link/":
            LINK_GETS.append(self.headers.get("Referer") or "")
            self._send(200, drive_page.helper_page().encode("utf-8"))
        elif path == "/picker.js":
            PICKER_GETS.append(1)
            self._send(200, FAKE_PICKER.encode("utf-8"), "text/javascript")
        elif path == "/fake/tap":
            from urllib.parse import parse_qs, urlsplit

            lab = parse_qs(urlsplit(self.path).query).get("l", [""])[0]
            go = bool(FAKE["tap"]) and lab == FAKE["tap"]   # only the page showing that button is tapped
            if go:
                FAKE["tap"] = 0
                FAKE["tapped"].append(lab)
            self._send(200, b"1" if go else b"0", "text/plain", cors=True)
        elif path == "/fake/pick":
            from urllib.parse import parse_qs, urlsplit

            q = {a: b[0] for a, b in parse_qs(urlsplit(self.path).query).items()}
            FAKE["picks"].append(q)
            how = FAKE["pick"].pop(0) if FAKE["pick"] else "cancel"
            if how == "office":
                GRANTS.add(OFFICE_ID)
                d = {"action": "picked", "docs": [{"id": OFFICE_ID, "name": FILES[OFFICE_ID]["name"],
                                                    "mimeType": "application/vnd.google-apps.folder"}]}
            elif how == "wrong":
                d = {"action": "picked", "docs": [{"id": "holidayphotos01", "name": "Holiday photos",
                                                    "mimeType": "application/vnd.google-apps.folder"}]}
            else:
                d = {"action": "cancel"}
            self._send(200, json.dumps(d).encode(), "application/json", cors=True)
        elif path.startswith("/drive/v3/files/"):
            if not self._auth():
                return
            fid = path.rsplit("/", 1)[1]
            if not visible(fid):
                drive_error(self, 404, "notFound")
                return
            f = FILES[fid]
            self._send(200, json.dumps({"id": fid, "name": f["name"], "mimeType": f["mimeType"],
                                        "trashed": bool(f.get("trashed")),
                                        "ownedByMe": f.get("own", True),
                                        "capabilities": {"canAddChildren": writable(fid)}}).encode(),
                       "application/json", cors=True)
        elif path == "/gsi.js":
            self._send(200, FAKE_GSI.encode("utf-8"), "text/javascript")
        elif path == "/drive/v3/about":
            ABOUT.append(self.headers.get("Authorization"))
            self._send(200, json.dumps({"user": {"emailAddress": "surveyor@example.com"}}).encode(),
                       "application/json", cors=True)
        elif path == "/drive/v3/files":
            if not self._auth():
                return
            from urllib.parse import parse_qs, urlsplit

            q = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
            files = [{"id": f["id"], "name": f["name"], "size": str(len(f["data"] or b"")),
                      "createdTime": f["createdTime"]} for f in drive_query(q)]
            self._send(200, json.dumps({"files": files}).encode(), "application/json", cors=True)
        elif path == "/p/":
            self._send(200, PHONE.encode("utf-8"))
        elif path == "/parent.html":
            self._send(200, PARENT_PAGE.encode("utf-8"))
        else:
            self._send(404)

    def _auth(self):
        if not (self.headers.get("Authorization") or "").startswith("Bearer tok-"):
            self._send(401, b"{}", "application/json", cors=True)
            return False
        return True

    def do_PATCH(self):  # noqa: N802   only {"trashed": true}: the file goes to its owner's bin
        if self._auth():
            fid = self.path.split("?", 1)[0].rsplit("/", 1)[1]
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if not visible(fid):
                drive_error(self, 404, "notFound")
                return
            if body.get("trashed") is True:
                path = drive_path(fid)
                FILES[fid]["trashed"] = True
                FAKE["binned"].append(fid)
                if os.path.isfile(path):   # Drive for desktop takes it out of the synced folder
                    os.remove(path)
            self._send(200, json.dumps({"id": fid}).encode(), "application/json", cors=True)

    def do_DELETE(self):  # noqa: N802
        if self._auth():
            fid = self.path.split("?", 1)[0].rsplit("/", 1)[1]
            if not visible(fid):
                drive_error(self, 404, "notFound")
                return
            FAKE["deleted"].append(fid)
            path = drive_path(fid)
            FILES.pop(fid, None)
            if os.path.isfile(path):
                os.remove(path)
            self._send(204, cors=True)

    def do_POST(self):  # noqa: N802
        if not self._auth():
            return
        path = self.path.split("?", 1)[0]
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if path == "/drive/v3/files":
            meta = json.loads(body)
            if parent_refused(self, meta):
                return
            f = drive_new(meta)
            self._send(200, json.dumps({"id": f["id"]}).encode(), "application/json", cors=True)
        elif path == "/upload/drive/v3/files":
            FAKE["times"].append(time.monotonic())
            if FAKE["fault"]:   # Google says no for now
                ft = FAKE["fault"].pop(0)
                if ft.get("kill"):
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                self.send_response(ft["code"])
                why = ft.get("reason", "backendError")
                out = json.dumps({"error": {"code": ft["code"], "errors": [{"reason": why}]}}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Expose-Headers", "Retry-After")
                if ft.get("ra"):
                    self.send_header("Retry-After", ft["ra"])
                self.end_headers()
                self.wfile.write(out)
                return
            b = self.headers.get("Content-Type", "").split("boundary=", 1)[1].encode()
            parts = body.split(b"--" + b)
            meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0])
            data = parts[2].split(b"\r\n\r\n", 1)[1][:-2]
            if parent_refused(self, meta):
                return
            f = drive_new(meta, data)
            FAKE["uploads"] += 1
            if FAKE["drop"]:
                FAKE["drop"] -= 1
                self._send(503, b"{}", "application/json", cors=True)   # stored, but the answer is lost
                return
            self._send(200, json.dumps({"id": f["id"], "size": str(len(data)),
                                        "sha256Checksum": hashlib.sha256(data).hexdigest()}).encode(),
                       "application/json", cors=True)
        else:
            self._send(404)


for port in (PA, PB, PC):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


POPUPS = []


class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        CONSOLE.append(message)
        print("js:", message, flush=True)

    def createWindow(self, _type):  # noqa: N802
        w = Page(self.profile())   # the link page opened as a window (where the office folder is chosen)
        POPUPS.append(w)
        w.windowCloseRequested.connect(lambda: (POPUPS.remove(w) if w in POPUPS else None, w.deleteLater()))
        return w   # window.close() really closes it, as a browser does


profile = QWebEngineProfile.defaultProfile()
from PyQt6.QtWebEngineCore import QWebEngineSettings  # noqa: E402

profile.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
result = {}


def js(page, expr):
    result.pop("v", None)
    page.runJavaScript(expr, lambda v: result.update(v=v))
    wait(lambda: "v" in result, 10)
    return result.get("v")


def got(page):
    return js(page, "JSON.stringify(window.got||[])") or "[]"


def tokens(page):
    return [m for m in json.loads(got(page)) if m.get("type") == "biruni-drive-token"]


# ---- the page and its CSP ------------------------------------------------------------------------------------------
html = drive_page.helper_page()
csp = html.split('Content-Security-Policy" content="', 1)[1].split('"', 1)[0]
script_src = [d for d in csp.split("; ") if d.startswith("script-src ")][0]
check("'unsafe-inline'" not in script_src and "default-src 'none'" in csp and "script-src 'sha256-" in csp
      and HELPER + "/gsi.js" in script_src and "https://docs.google.com" in csp,
      "the link page has a CSP: its own script by hash, the sign-in and Picker scripts only")
check(PARENT in html and "*'" not in html.split("<script>", 1)[1], "it posts only to the photo page's exact origin")

# ---- opened alone: it only explains --------------------------------------------------------------------------------
alone = Page(profile)
alone.load(QUrl(HELPER + "/link/"))
check(wait(lambda: js(alone, "document.getElementById('alone')&&!document.getElementById('alone').hidden") is True, 15)
      and js(alone, "document.getElementById('ui').hidden") is True, "opened alone: an explanation, no button")

# ---- framed by the photo page --------------------------------------------------------------------------------------
p = Page(profile)
p.load(QUrl(PARENT + "/parent.html"))
check(wait(lambda: len(tokens(p)) >= 1, 20), "framed by the photo page: a token arrives " + got(p))
t = (tokens(p) or [{}])[0]
nonce = js(p, "window.NONCE")
check(t.get("token") == "tok-1" and t.get("nonce") == nonce and t.get("cached") is False and t.get("v") == 1
      and 3500e3 < (t.get("exp") or 0) - time.time() * 1000 < 3700e3, f"… for this page's nonce, with its hour ({t})")
modes = [m.get("mode") for m in json.loads(got(p)) if m.get("type") == "biruni-drive-ready"]
check(modes[:1] == ["iframe"], f"the link page says it is framed ({modes})")
check(wait(lambda: "Bearer tok-1" in ABOUT, 10), "it asks Drive who signed in (the email stays on the link page)")
check(not any("email" in json.dumps(m) or "example.com" in json.dumps(m) for m in json.loads(got(p))),
      "the email never reaches the photo page")

# ---- a reload: the cached token, without a new sign-in -------------------------------------------------------------
p.load(QUrl(PARENT + "/parent.html?again"))
check(wait(lambda: any(x.get("cached") for x in tokens(p)), 20), "after a reload the cached token comes " + got(p))
c = next((x for x in tokens(p) if x.get("cached")), {})
check(c.get("token") == "tok-1" and c.get("nonce") == js(p, "window.NONCE") and c.get("nonce") != nonce,
      "… the same token, for the new nonce")

# ---- a bad nonce, a stranger ---------------------------------------------------------------------------------------
bad = Page(profile)
bad.load(QUrl(PARENT + "/parent.html?bad"))
wait(lambda: False, 3)
msgs = json.loads(got(bad))
check(any(m.get("type") == "biruni-drive-ready" for m in msgs) and not tokens(bad), "a hello with a bad nonce gets nothing")
stranger = Page(profile)
stranger.load(QUrl(STRANGER + "/parent.html"))
wait(lambda: False, 3)
check(js(stranger, "document.querySelector('iframe')!==null") is True and got(stranger) == "[]",
      "a stranger framing the link page gets nothing at all, not even ready")

# ---- the phone page: its Drive card (7c) ---------------------------------------------------------------------------
from biruni.field_photos.handover import HandoverKey  # noqa: E402
import tempfile  # noqa: E402

KEYDIR = tempfile.mkdtemp()
pk = HandoverKey(KEYDIR).public
k = bridge.new_key()
from biruni.field_photos import drive  # noqa: E402

OFFICE_PATH, made = drive.ensure_office_folder([DRIVE_DISK], k)
OFFICE_ID = "officefolder0001"
FILES[OFFICE_ID] = {"id": OFFICE_ID, "name": os.path.basename(OFFICE_PATH), "mimeType": "application/vnd.google-apps.folder",
                    "parents": [], "appProperties": {}, "data": None, "createdTime": "2026-11-16T00:00:00Z",
                    "app": False, "own": False}
check(made and os.path.isdir(OFFICE_PATH), "the office computer made its folder in Drive for desktop")
plain = Page(profile)
n_link = len(LINK_GETS)
plain.load(QUrl(f"{PARENT}/p/#k={k}&i={bridge.pin(k)}&l=en"))
wait(lambda: js(plain, "typeof DRV") == "object", 15)
wait(lambda: False, 2)
check(js(plain, "document.getElementById('driveCard')===null&&!DRV.on") is True and len(LINK_GETS) == n_link,
      "a link without d=1: no Drive card, and the link page is never asked for")
ph = Page(profile)
ph.load(QUrl(f"{PARENT}/p/#k={k}&i={bridge.pin(k)}&l=ar&d=1&pk={pk}"))
check(wait(lambda: js(ph, "!!document.getElementById('driveCard')") is True, 15), "with d=1&pk=: the Drive card")
check(js(ph, "PCKEY") == pk, "… and photos are sealed for that key from the start")
check(wait(lambda: js(ph, "DRV.ok()") is True, 20), "the link page hands its token to the phone page "
      + str(js(ph, "JSON.stringify({s:DRV.state,r:DRV.ready})")))
check(wait(lambda: js(ph, "DRV.state") == "pick", 10)
      and js(ph, "document.querySelector('#driveCard .dstate').textContent")
      == js(ph, "T.drivePickHint.replace('{name}',DRV.fname())") and js(ph, "T.drivePickHint") != ""
      and js(ph, "!document.querySelector('#driveCard .dpick').classList.contains('hide')") is True,
      "linked, the card asks to choose the office folder, in the page's language")
check(js(ph, "document.querySelector('#driveCard iframe').src") == HELPER + "/link/",
      "the link is a frame of the link page (not Google's script in the photo page)")
check(js(ph, "[...document.scripts].every(s=>!s.src)&&typeof google==='undefined'") is True,
      "the photo page itself loads no outside script, and Google's object never exists in it")
check(all(not r.startswith(PARENT + "/p/#") and "k=" not in r for r in LINK_GETS),
      "the photo page's address (with the key after #) never reaches the link page")
ph.runJavaScript("window.postMessage({v:1,type:'biruni-drive-token',nonce:DRV.nonce,token:'forged',exp:Date.now()+9e6},'*')")
wait(lambda: False, 1)
check(js(ph, "DRV.tok") != "forged", "a token posted by the page to itself (not from the link frame) is ignored")

# ---- 7d: a photo taken with the computer out of reach goes to Drive, and the computer takes it in ---------------------
import queue  # noqa: E402

from biruni.field_photos.server import PhotoStore  # noqa: E402


def take(pid):
    ph.runJavaScript("(async()=>{await put({id:'%s',blob:new Blob([new Uint8Array([255,216,255,224].concat("
                     "Array.from({length:3000},(_,i)=>i%%251)))],{type:'image/jpeg'}),"
                     "meta:{id:'%s',t:Date.now(),lat:24.7,lon:46.7,acc:4}});drivePump()})()" % (pid, pid))


def bfps():
    return sorted(os.path.relpath(os.path.join(d, n), DRIVE_DISK).replace(os.sep, "/")
                  for d, _, fs in os.walk(DRIVE_DISK) for n in fs)


def ajs(page, expr):
    """The value of an async expression (runJavaScript does not wait for promises)."""
    page.runJavaScript("window.__r=undefined;(async()=>{try{window.__r=JSON.stringify(await (%s))}"
                       "catch(e){window.__r=JSON.stringify('error '+e)}})()" % expr)
    wait(lambda: js(page, "window.__r!==undefined") is True, 10)
    v = js(page, "window.__r")
    return json.loads(v) if isinstance(v, str) else None


def queued(pid):
    return ajs(ph, "all().then(a=>a.some(x=>x.id==='%s'))" % pid)


take("photo-drive-0001")
wait(lambda: False, 2)
check(not bfps() and queued("photo-drive-0001") is True and js(ph, "DRV.state") == "pick"
      and not any(f["name"].startswith("biruni-photos-") and f.get("app", True) for f in FILES.values()),
      "before the office folder is chosen: the photo waits, and the phone makes no folder of its own")
check(not PICKER_GETS, "the small link frame never loads Google Picker")
ph.runJavaScript("window.postMessage({v:1,type:'biruni-drive-folder',nonce:DRV.nonce,id:'%s',name:DRV.fname()},'*')"
                 % OFFICE_ID)
wait(lambda: False, 1)
check(js(ph, "DRV.fold") is None, "a folder message from the page itself is ignored")

# the surveyor taps "Choose the office folder": the link page opens as a window with Picker; a wrong folder first
FAKE["pick"] = ["wrong", "office"]
ph.runJavaScript("driveOpenPick()")
check(wait(lambda: js(ph, "!!DRV.fold") is True, 30),
      f"the office folder is chosen in the link window ({FAKE['picks']}, popups {len(POPUPS)}, state {js(ph, 'DRV.state')})")
pq = FAKE["picks"]
check(len(pq) == 2 and all(x.get("q") == FILES[OFFICE_ID]["name"] and x.get("t", "").startswith("tok-")
                           and x.get("a") == "890626226614" for x in pq),
      "Picker is asked for the office folder by name, with the phone's token and the app id")
check(js(ph, "DRV.fold.r") == OFFICE_ID and js(ph, "DRV.fold.own") is False
      and js(ph, "JSON.parse(kept('biruni-drive-f-'+ROOM)).k") == 1,
      "a wrong folder is refused in the window; the office's folder is kept for this room")
check(wait(lambda: len(bfps()) == 1, 20), f"the photo is uploaded to Drive ({bfps()}, state {js(ph, 'DRV.state')})")
tag, dev = drive.room_tag(k), js(ph, "DEVID")
check(FILES[OFFICE_ID]["name"] == f"biruni-photos-{tag}", "the office folder has the room's name")
check(bool(bfps()) and bfps()[0].startswith(f"biruni-photos-{tag}/p-{dev}/000001-"),
      "… as biruni-photos-<tag>/p-<device>/000001-<sha>.bfp")
check(wait(lambda: js(ph, "DRV.n") == 1, 10), "the card counts it")
check(wait(lambda: queued("photo-drive-0001") is False, 10), "it left the phone's queue")
store = PhotoStore(tempfile.mkdtemp())
pc = HandoverKey(KEYDIR)
r1 = drive.DriveIntake([DRIVE_DISK], store, pc, k, queue.Queue()).scan()
rec = store.records.get("photo-drive-0001") or {}
check(r1["new"] == 1 and rec.get("route") == "drive" and rec.get("lat") == 24.7,
      f"Drive for desktop's copy opens on the computer: the photo, its place, route drive ({r1})")

# the answer to an upload is lost: the retry finds the file and does not upload it again
FAKE["drop"] = 1
up0 = FAKE["uploads"]
take("photo-drive-0002")
wait(lambda: FAKE["uploads"] > up0, 15)
wait(lambda: False, 1)
ph.runJavaScript("drivePump()")
check(wait(lambda: js(ph, "DRV.n") == 2, 20) and FAKE["uploads"] == up0 + 1 and len(bfps()) == 2,
      f"a lost answer: the retry finds the file in Drive, no second copy (uploads {FAKE['uploads'] - up0})")

# a photo the line is sending is not uploaded too; one held by Drive is skipped by the line
ph.runJavaScript("DRV.HOLD.set('photo-drive-0003','line')")
take("photo-drive-0003")
wait(lambda: False, 3)
check(len(bfps()) == 2 and queued("photo-drive-0003") is True, "a photo the bridge is sending is not uploaded to Drive too")
check(ajs(ph, "(async()=>{DRV.HOLD.set('x-held','drive');const r=await sendItem({id:'x-held'});"
              "DRV.HOLD.delete('x-held');return r})()") == 0, "the bridge skips a photo Drive is sending (0)")
ph.runJavaScript("DRV.HOLD.delete('photo-drive-0003');drivePump()")
check(wait(lambda: len(bfps()) == 3, 15), "… and once the bridge lets go, it goes to Drive")
check(ajs(ph, "dget('#key').then(k=>k.kp.privateKey.extractable)") is False,
      "the phone's signing key cannot be taken out of the browser")
r2 = drive.DriveIntake([DRIVE_DISK], store, pc, k, queue.Queue()).scan()
check(r2["new"] == 2 and not r2["refused"], f"the computer takes in the other two, nothing refused ({r2})")

# p-<device> was deleted in Drive: it is made again, no new choice needed
pdev = [i for i, f in FILES.items() if f["name"] == f"p-{dev}"]
for i in pdev:
    FILES.pop(i)
n0 = len(bfps())
take("photo-drive-0005")
wait(lambda: False, 2)
ph.runJavaScript("drivePump()")
check(wait(lambda: js(ph, "DRV.n") == 4, 20) and len([f for f in FILES.values() if f["name"] == f"p-{dev}"]) == 1
      and js(ph, "!!DRV.fold") is True, f"a deleted p-<device> is made again, no new choice (state {js(ph, 'DRV.state')})")


def repick():
    FAKE["pick"] = ["office"]
    ph.runJavaScript("driveOpenPick()")
    ok = wait(lambda: js(ph, "!!DRV.fold") is True, 30)
    wait(lambda: False, 0.5)
    ph.runJavaScript("drivePump()")   # what the 15 s timer would do
    return ok


# the office shares for viewing only: the card says so, the photo waits; made an Editor and chosen again, it goes
FILES[OFFICE_ID]["canAdd"] = False
take("photo-drive-0006")
check(wait(lambda: js(ph, "DRV.state") == "ro", 20) and js(ph, "DRV.fold") is None and queued("photo-drive-0006") is True,
      f"a view-only share: the card asks the office for Editor, the photo waits (state {js(ph, 'DRV.state')})")
FILES[OFFICE_ID]["canAdd"] = True
check(repick() and wait(lambda: js(ph, "DRV.n") == 5, 20),
      f"made an Editor and chosen again: the photo goes (n {js(ph, 'DRV.n')}, state {js(ph, 'DRV.state')}, "
      f"until {js(ph, 'DRV.until-Date.now()')})")

# the share is taken back: the folder is gone for the phone; shared and chosen again, the same p-<device> is used
FILES[OFFICE_ID]["revoked"] = True
take("photo-drive-0007")
check(wait(lambda: js(ph, "DRV.state") == "gone", 20) and queued("photo-drive-0007") is True,
      f"the share taken back: the card says the folder cannot be reached, the photo waits (state {js(ph, 'DRV.state')})")
FILES[OFFICE_ID]["revoked"] = False
check(repick() and wait(lambda: js(ph, "DRV.n") == 6, 20)
      and len([f for f in FILES.values() if f["name"] == f"p-{dev}"]) == 1,
      f"shared and chosen again: the photo goes, into the same p-<device> (n {js(ph, 'DRV.n')}, state "
      f"{js(ph, 'DRV.state')}, fails {js(ph, 'DRV.fails')}, until {js(ph, 'DRV.until-Date.now()')}, "
      f"p-dev {len([f for f in FILES.values() if f['name'] == f'p-{dev}'])})")
# the office folder is put in the bin (Drive still takes uploads into it): the phone notices within 5 minutes
FILES[OFFICE_ID]["trashed"] = True
n0, nb = js(ph, "DRV.n"), len(bfps())
ph.runJavaScript("DRV.fold.v=Date.now()-DCHECK-1")
take("photo-drive-0008")
check(wait(lambda: js(ph, "DRV.state") == "gone", 20) and js(ph, "DRV.fold") is None and len(bfps()) == nb
      and queued("photo-drive-0008") is True,
      f"the office folder in the bin: the card says it cannot be reached, nothing goes into the bin (state {js(ph, 'DRV.state')})")
FILES[OFFICE_ID]["trashed"] = False
check(repick() and wait(lambda: js(ph, "DRV.n") == n0 + 1, 20), "taken out of the bin and chosen again: the photo goes")
check(js(ph, "DRV.fold.v") > 0, "a freshly chosen folder is not checked again at once")

# the line showed photo 5 as a preview first (Drive is the main way): Drive's copy replaces it in the same record
store.save({"id": "photo-drive-0005", "lat": 24.7, "lon": 46.7, "t": 1, "preview": True},
           b"\xff\xd8\xff\xe0small preview\xff\xd9")
check((store.records.get("photo-drive-0005") or {}).get("preview") is True, "the preview from the line is on the computer")
r3 = drive.DriveIntake([DRIVE_DISK], store, pc, k, queue.Queue()).scan()
p5 = store.records.get("photo-drive-0005") or {}
check(not p5.get("preview") and p5.get("route") == "drive" and os.path.getsize(p5.get("photo", "")) > 3000,
      f"Drive's original replaces the preview: full size, route drive ({p5.get('preview')}, {p5.get('route')})")
check(r3["new"] == 4 and not r3["refused"], f"the computer takes in those three too ({r3})")
check(ajs(ph, "(()=>{const o=kept('biruni-drive-f-'+ROOM);keep('biruni-drive-f-'+ROOM,JSON.stringify({t:DRV.tag,r:'x',p:'y'}));"
              "const c=dcache();keep('biruni-drive-f-'+ROOM,o);return c})()") is None,
      "a folder kept by the older page (not chosen) is not used")

# ---- step 8: Drive says not now: the phone waits longer each time; Google's Retry-After comes first ----
def gaps():
    t = FAKE["times"]
    return [round(b - a, 2) for a, b in zip(t, t[1:])]


ph.runJavaScript("DRV.BACK=200;dsoon()")
nb = len(bfps())
FAKE["times"], FAKE["fault"] = [], [{"code": 429, "ra": "2"}]
take("photo-drive-0011")
check(wait(lambda: len(bfps()) == nb + 1, 20) and len(FAKE["times"]) == 2 and gaps()[0] >= 1.9,
      f"429 with Retry-After 2: no new try before 2 s, then one file ({gaps()})")
check(ajs(ph, "[ddelay(1),ddelay(3),ddelay(20),ddelay(1,'5'),ddelay(1,'9999')]") is not None and
      (lambda d: 200 <= d[0] < 300 and 800 <= d[1] < 900 and 64000 <= d[2] < 64100 and d[3] >= 5000 and d[4] == 600000)(
          ajs(ph, "[ddelay(1),ddelay(3),ddelay(20),ddelay(1,'5'),ddelay(1,'9999')]")),
      "the waits double from the start up to 64 s; Retry-After is kept up to 10 minutes")
ph.runJavaScript("DRV.BACK=1500")
FAKE["times"], FAKE["fault"] = [], [{"code": 503}, {"code": 503}, {"code": 503}]
take("photo-drive-0012")
check(wait(lambda: len(bfps()) == nb + 2, 45) and len(FAKE["times"]) == 4
      and all(g >= m for g, m in zip(gaps(), (1.5, 3.0, 6.0))),
      f"503 three times: each wait at least doubles (1.5, 3, 6 s), then one file ({gaps()})")
FAKE["times"], FAKE["fault"] = [], [{"code": 403, "reason": "userRateLimitExceeded"}]
take("photo-drive-0013")
check(wait(lambda: len(bfps()) == nb + 3, 20) and js(ph, "!!DRV.fold") is True,
      "a 403 rate limit is only a wait: the folder is kept and the photo goes")
FAKE["times"], FAKE["fault"] = [], [{"kill": 1}]
take("photo-drive-0014")
check(wait(lambda: len(bfps()) == nb + 4, 20) and len(FAKE["times"]) == 2,
      "the connection cut mid-upload: tried again, one file")
check(wait(lambda: js(ph, "DRV.fails") == 0 and js(ph, "DRV.until") == 0, 5),
      f"a success starts the waits over ({js(ph, 'DRV.fails')}, {js(ph, 'DRV.until')}, {js(ph, 'DRV.state')})")

# a refused token: the card asks for a new link, nothing is sent
n0 = len(bfps())
ph.runJavaScript("DRV.tok='bad-token';DRV.exp=Date.now()+9e6")
take("photo-drive-0004")
check(wait(lambda: js(ph, "DRV.state") == "auth", 15) and len(bfps()) == n0 and queued("photo-drive-0004") is True,
      "a refused token: the card asks to link again, the photo waits")
ph.runJavaScript("drivePump()")
SEND1 = js(ph, "String(T.driveSend).replace('{n}',1)")   # in the page's own language
check(wait(lambda: js(ph, "DRV.label") == SEND1, 10),
      f"… its button counts the photos waiting: Send photos (1) ({js(ph, 'DRV.label')})")
FAKE["tap"] = SEND1
check(wait(lambda: len(bfps()) == n0 + 1, 25) and FAKE["tapped"] == [SEND1],
      f"one tap on it: a new token and the photo goes ({FAKE['tapped']}, state {js(ph, 'DRV.state')})")

ph.runJavaScript("(()=>{document.body.style.width='360px';const p=$('waitPill');"
                 "p.textContent='12 '+(T.drivePreview||'x').repeat(12);p.classList.remove('hide')})()")
wait(lambda: False, 0.5)
check(js(ph, "(()=>{const t=document.querySelector('.top');return t.scrollWidth<=t.clientWidth&&t.clientWidth<=360})()") is True,
      "a long waiting count never makes the phone's top bar wider than the screen")

# a photo is kept at 5 MB at most: a bigger camera photo is saved again smaller, a small one is kept as it came
fit = ajs(ph, "(async()=>{const c=document.createElement('canvas');c.width=4000;c.height=3000;const x=c.getContext('2d'),"
              "d=x.createImageData(4000,3000);for(let i=0;i<d.data.length;i++)d.data[i]=(i*2654435761>>>13)&255;"
              "x.putImageData(d,0,0);const raw=await new Promise(r=>c.toBlob(r,'image/jpeg',1));"
              # a camera EXIF block: little-endian, IFD0 with Orientation 6 and Make 'TestCam'
              "const t=[0x49,0x49,42,0,8,0,0,0, 2,0, 0x12,0x01,3,0,1,0,0,0,6,0,0,0, 0x0f,0x01,2,0,8,0,0,0,38,0,0,0,"
              " 0,0,0,0].concat(Array.from('TestCam').map(ch=>ch.charCodeAt(0)),[0]),"
              "body=[0x45,0x78,0x69,0x66,0,0].concat(t),seg=new Uint8Array([0xff,0xe1,(body.length+2)>>8,(body.length+2)&255].concat(body)),"
              "big=await withExif(raw,seg);"
              "const out=await fitPhoto(big),bm=await createImageBitmap(out),small=new Blob([new Uint8Array(3000)]),"
              "ex=jpegExif(new Uint8Array(await out.arrayBuffer()));"
              "return {big:big.size,out:out.size,w:bm.width,type:out.type,same:(await fitPhoto(small))===small,"
              "exif:!!ex&&new TextDecoder().decode(ex).includes('TestCam'),orient:ex?ex[10+8+2+8]:null}})()")
check(isinstance(fit, dict) and fit["big"] > 5 * 1024 * 1024 and fit["out"] <= 5 * 1024 * 1024 - 65536
      and fit["w"] >= 1200 and fit["type"] == "image/jpeg" and fit["same"],
      f"a camera photo over 5 MB is saved again under 5 MB and still opens; a small one is not touched ({fit})")
check(isinstance(fit, dict) and fit["exif"] and fit["orient"] == 1,
      "the camera's EXIF (make, model...) goes with it, set upright since the pixels already are")

# ---- step 10: the computer's receipt frees the surveyor's Drive: manual (a button), auto (at once), keep ----
# the PC's identity key, as the bridge's QR code pins it (here made in the page itself)
ajs(ph, "(async()=>{const kp=await crypto.subtle.generateKey({name:'ECDSA',namedCurve:'P-256'},false,['sign','verify']);"
        "ID.sk=kp.privateKey;const raw=new Uint8Array(await crypto.subtle.exportKey('raw',kp.publicKey));"
        "ID.pub=b64u.enc(raw);ID.pin=b64u.enc(await sha(raw)).slice(0,22);return 1})()")


def receipt(clean, forge=False):
    """the PC says it holds every photo uploaded so far, as a full original -> how many may now be freed"""
    return ajs(ph, "(async()=>{const ids=(await dall()).filter(r=>r.st==='done'&&r.fid).map(r=>r.id);"
                   "const nonce='n'+Math.random().toString(36).slice(2);DRV.rq={nonce:nonce,ids:new Set(ids)};"
                   "const g=await IDSIGN('BRDG1|'+nonce+'|%s|'+ids.join(','));"
                   "await driveGot({t:'dgot',nonce:nonce,clean:'%s',ids:ids,ip:g.pub,is:%s});"
                   "return (await dall()).filter(r=>r.st==='got').length})()"
               % (clean, clean, "(g.sig[0]==='A'?'B':'A')+g.sig.slice(1)" if forge else "g.sig"))


def free_btn():
    return js(ph, "(()=>{const b=document.querySelector('.dfree');return b.classList.contains('hide')?'':b.textContent})()")


nd = ajs(ph, "dall().then(a=>a.filter(r=>r.st==='done'&&r.fid).length)")
check(nd and receipt("manual", forge=True) == 0, f"a receipt not signed by the computer frees nothing ({nd} uploaded)")
nb, dn = len(bfps()), js(ph, "DRV.n")
check(receipt("manual") == nd and FAKE["binned"] == [] and free_btn() == js(ph, f"String(T.driveFree).replace('{{n}}',{nd})"),
      f"manual: the receipt shows the button, nothing deleted yet ({free_btn()!r})")
ph.runJavaScript("document.querySelector('.dfree').click()")
t_free = time.monotonic()
check(wait(lambda: len(FAKE["binned"]) == nd and all(FILES[f].get("trashed") for f in FAKE["binned"])
           and free_btn() == "", 20) and js(ph, "DRV.n") == dn and FAKE["deleted"] == [],
      f"one tap: those {nd} files go to the surveyor's bin (not deleted), the button goes ({len(FAKE['binned'])}, "
      f"disk {nb}->{len(bfps())}, n {dn}->{js(ph, 'DRV.n')}, button {free_btn()!r}, {time.monotonic() - t_free:.1f} s)")
check(ajs(ph, "dall().then(a=>a.filter(r=>r.st==='freed').length)") == nd, "… and the phone notes them as freed")
nb = len(bfps())
take("photo-drive-0016")
up1 = lambda: ajs(ph, "dall().then(a=>a.filter(r=>r.st==='done'&&r.fid).length)") == 1   # noqa: E731
check(wait(lambda: len(bfps()) == nb + 1 and up1(), 20) and receipt("auto") >= 0
      and wait(lambda: len(FAKE["binned"]) == nd + 1 and len(bfps()) == nb and free_btn() == "", 15),
      "auto: the receipt puts the copy in the bin at once, no button")
take("photo-drive-0017")
kp = wait(lambda: len(bfps()) == nb + 1 and up1(), 20) and receipt("keep")
check(kp == 1 and free_btn() == "" and not wait(lambda: len(FAKE["binned"]) > nd + 1, 2),
      f"keep: nothing goes to the bin and no button shows ({kp}, {free_btn()!r}, {len(FAKE['binned']) - nd})")

check(not any("Content Security Policy" in m for m in CONSOLE), "nothing was refused by the CSP")
print(f"\n{'FAILED ' + str(len(failures)) if failures else 'ALL PASSED'}")
sys.exit(1 if failures else 0)
