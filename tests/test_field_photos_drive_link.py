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
                   "BIRUNI_DRIVE_GSI": HELPER + "/gsi.js"})
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
from biruni.field_photos import drive_page  # noqa: E402

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
 if(b&&!b.disabled&&!window.__tapped){window.__tapped=1;b.click()}else setTimeout(poll,50)})();
"""

PARENT_PAGE = r"""<!doctype html><html><body><script>
window.got=[];const bad=location.search.indexOf('bad')>=0,NONCE=bad?'x':'n'+Math.random().toString(36).slice(2,14);
const f=document.createElement('iframe');f.src='__HELPER__/link/';document.body.appendChild(f);
addEventListener('message',e=>{if(e.source!==f.contentWindow)return;got.push(e.data);
 if(e.data&&e.data.type==='biruni-drive-ready')
  f.contentWindow.postMessage({v:1,type:'biruni-drive-hello',nonce:NONCE,label:'Link Google Drive',dir:'ltr'},'__HELPER__')});
window.NONCE=NONCE;
</script></body></html>""".replace("__HELPER__", HELPER)


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
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, cors=True)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/link/":
            self._send(200, drive_page.helper_page().encode("utf-8"))
        elif path == "/gsi.js":
            self._send(200, FAKE_GSI.encode("utf-8"), "text/javascript")
        elif path == "/drive/v3/about":
            ABOUT.append(self.headers.get("Authorization"))
            self._send(200, json.dumps({"user": {"emailAddress": "surveyor@example.com"}}).encode(),
                       "application/json", cors=True)
        elif path == "/parent.html":
            self._send(200, PARENT_PAGE.encode("utf-8"))
        else:
            self._send(404)


for port in (PA, PB, PC):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        CONSOLE.append(message)
        print("js:", message, flush=True)


profile = QWebEngineProfile.defaultProfile()
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
check("'unsafe-inline'" not in csp and "default-src 'none'" in csp and f"script-src 'sha256-" in csp
      and HELPER + "/gsi.js" in csp, "the link page has a CSP: its own script by hash and the sign-in script only")
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

check(not any("Content Security Policy" in m for m in CONSOLE), "nothing was refused by the CSP")
print(f"\n{'FAILED ' + str(len(failures)) if failures else 'ALL PASSED'}")
sys.exit(1 if failures else 0)
