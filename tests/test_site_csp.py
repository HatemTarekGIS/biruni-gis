"""Content-Security-Policy on every page of the public Biruni site (secure/csp.py).

Builds the site into a temporary folder with scripts/build_bridge_page.py, adds paper/ when present,
runs csp.apply_site(), checks every HTML file statically (meta order, hashes, directives, frame-src),
tests add_csp() error cases and idempotency, and loads every offline page in Chromium (QtWebEngine)
to verify no CSP violations occur and an injected Drive iframe + inline script are blocked.

Run:  engine\\bin\\python-qgis.bat tests\\test_site_csp.py
"""
import base64
from functools import partial
import hashlib
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --no-sandbox"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

from biruni.field_photos import bridge  # noqa: E402
from biruni.secure import csp  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


# ---- build site into temp folder -----------------------------------------------------------------------------------
WORK = tempfile.mkdtemp(prefix="biruni-csp-")
TMP = os.path.join(WORK, "site")
PYTHON = sys.executable

proc = subprocess.run(
    [PYTHON, os.path.join(ROOT, "scripts", "build_bridge_page.py"), TMP],
    cwd=ROOT,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    timeout=120,
)
check(proc.returncode == 0, f"build_bridge_page.py builds the site (exit {proc.returncode})")
if proc.returncode != 0:
    print(proc.stdout[-1500:])
    print(proc.stderr[-1500:])

paper_src = os.path.join(ROOT, "build", "bridge-page", "paper")
has_paper = os.path.isdir(paper_src)
if has_paper:
    shutil.copytree(paper_src, os.path.join(TMP, "paper"), dirs_exist_ok=True)
for name in ("index.html", "privacy.html"):
    dst = os.path.join(TMP, name)
    if not os.path.isfile(dst):
        shutil.copy2(os.path.join(ROOT, "docs", "site", name), dst)

rewritten = csp.apply_site(TMP)
check(len(rewritten) >= 8, f"csp.apply_site() rewrote {len(rewritten)} HTML files")


# ---- static checks -------------------------------------------------------------------------------------------------
class _PageAudit(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=False)
        self.html = html
        self.starts = [0]
        pos = 0
        while True:
            idx = html.find("\n", pos)
            if idx < 0:
                break
            pos = idx + 1
            self.starts.append(pos)
        self.csp_metas = []
        self.first_resource_pos = None
        self.script_hashes = []
        self.style_hashes = []
        self._open = None

    def _off(self):
        line, col = self.getpos()
        return self.starts[line - 1] + col

    def _inspect(self, tag, attrs):
        t = tag.lower()
        amap = {k.lower(): v for k, v in attrs}
        if t in ("script", "style", "link") and self.first_resource_pos is None:
            self.first_resource_pos = self._off()
        if t == "meta" and (amap.get("http-equiv") or "").strip().lower() == "content-security-policy":
            self.csp_metas.append((self._off(), amap.get("content") or ""))
        return amap

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        amap = self._inspect(t, attrs)
        if t == "script" and "src" not in amap:
            self._open = ("script", self._off() + len(self.get_starttag_text() or ""))
        elif t == "style":
            self._open = ("style", self._off() + len(self.get_starttag_text() or ""))

    def handle_startendtag(self, tag, attrs):
        self._inspect(tag.lower(), attrs)

    def handle_endtag(self, tag):
        t = tag.lower()
        if self._open and self._open[0] == t:
            body = self.html[self._open[1]:self._off()]
            h = "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii") + "'"
            if t == "script":
                self.script_hashes.append(h)
            else:
                self.style_hashes.append(h)
            self._open = None


BAD_SCRIPT_TOKENS = {"'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'", "*", "https:", "data:", "blob:"}

html_files = []
for root, dirs, names in os.walk(TMP):
    dirs[:] = sorted(d for d in dirs if d != ".git")
    for name in sorted(names):
        if name.lower().endswith(".html"):
            html_files.append(os.path.join(root, name))

for path in html_files:
    rel = os.path.relpath(path, TMP).replace(os.sep, "/")
    with open(path, "r", encoding="utf-8", newline="") as f:
        html = f.read()
    audit = _PageAudit(html)
    audit.feed(html)
    audit.close()

    one_meta = len(audit.csp_metas) == 1
    before_res = (one_meta and (audit.first_resource_pos is None
                                or audit.csp_metas[0][0] < audit.first_resource_pos))
    check(one_meta and before_res, f"{rel}: exactly one CSP meta before any <script>, <style> or <link>")

    policy = audit.csp_metas[0][1] if one_meta else ""
    directives = [d.strip() for d in policy.split(";") if d.strip()]
    dmap = {}
    for d in directives:
        parts = d.split()
        dmap[parts[0].lower()] = parts[1:]

    check(
        dmap.get("default-src") == ["'none'"]
        and dmap.get("base-uri") == ["'none'"]
        and dmap.get("form-action") == ["'none'"],
        f"{rel}: default-src 'none', base-uri 'none', form-action 'none'",
    )

    script_tokens = dmap.get("script-src", [])
    check(
        not any(tok in BAD_SCRIPT_TOKENS for tok in script_tokens),
        f"{rel}: script-src has no unsafe or wildcard tokens",
    )

    style_tokens = dmap.get("style-src", [])
    hashes_ok = (all(h in script_tokens for h in audit.script_hashes)
                 and all(h in style_tokens for h in audit.style_hashes))
    check(
        hashes_ok,
        f"{rel}: all inline script ({len(audit.script_hashes)}) and style ({len(audit.style_hashes)}) hashes in policy",
    )

    frame_tokens = dmap.get("frame-src", [])
    if rel != "p/index.html":
        check(
            frame_tokens in (["'none'"], ["'self'"]) and not any("drive.biruni-gis.com" in t for t in frame_tokens),
            f"{rel}: frame-src is 'none' or 'self' without drive.biruni-gis.com ({frame_tokens})",
        )
    else:
        check(
            any("drive.biruni-gis.com" in t for t in frame_tokens),
            f"{rel}: keeps drive.biruni-gis.com in frame-src",
        )


# ---- unit checks ---------------------------------------------------------------------------------------------------
BASE_HTML = (
    '<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0}</style></head>'
    "<body><script>1+1</script></body></html>"
)


def raises_value_error(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


check(
    raises_value_error(lambda: csp.add_csp(BASE_HTML.replace("<body>", '<body onclick="alert(1)">'))),
    "add_csp raises on onclick= attribute",
)
check(
    raises_value_error(
        lambda: csp.add_csp(BASE_HTML.replace("</head>", '<script src="https://evil.invalid/x.js"></script></head>'))
    ),
    "add_csp raises on unknown external script",
)
check(
    raises_value_error(lambda: csp.add_csp(BASE_HTML.replace("<head>", "<head>\r\n"))),
    "add_csp raises on CR (\\r)",
)
once = csp.add_csp(BASE_HTML)
twice = csp.add_csp(once)
check(once == twice, "add_csp is idempotent (running twice gives the same result)")

old_relays, old_ntfy = bridge.RELAYS, bridge.NTFY
try:
    bridge.RELAYS = ("wss://relay.example.test/nostr",)
    bridge.NTFY = "https://ntfy.example.test/topic"
    pol = csp.site_policies()
    check(
        pol["s/index.html"]["connect"]
        == ("wss://relay.example.test", "https://ntfy.example.test", "wss://ntfy.example.test"),
        "site_policies() reflects bridge.RELAYS and bridge.NTFY at call time",
    )
finally:
    bridge.RELAYS, bridge.NTFY = old_relays, old_ntfy


# ---- dynamic checks in offscreen Chromium --------------------------------------------------------------------------
from PyQt6.QtCore import QCoreApplication, Qt, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402

app = QGuiApplication(sys.argv[:1])


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=TMP))
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
BASE_URL = f"http://127.0.0.1:{PORT}"


def wait(cond, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


class CspPage(QWebEnginePage):
    def __init__(self, profile):
        super().__init__(profile)
        self.console = []

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        self.console.append(message)


def run_js(page, expr):
    box = {}
    page.runJavaScript(expr, lambda v: box.update(v=v))
    wait(lambda: "v" in box, 10)
    return box.get("v")


profile = QWebEngineProfile.defaultProfile()
routes = ["/", "/r/", "/v/", "/s/", "/d/", "/portal/", "/privacy.html"]
if has_paper:
    routes.append("/paper/")

print("NOTE skipping /m/ in dynamic check (needs internet)", flush=True)

for route in routes:
    pg = CspPage(profile)
    loaded = {"ok": False}
    pg.loadFinished.connect(lambda ok, d=loaded: d.update(ok=ok))
    pg.load(QUrl(BASE_URL + route))
    wait(lambda d=loaded: d["ok"], 15)
    wait(lambda: False, 3.0)

    csp_msgs = [m for m in pg.console if re.search(r"Content Security Policy", m)]
    check(not csp_msgs, f"dynamic {route}: no Content Security Policy console errors ({csp_msgs[:2]})")

    run_js(
        pg,
        "document.body.insertAdjacentHTML('beforeend','<iframe src=\"https://drive.biruni-gis.com/link/\"></iframe>');"
        "var s=document.createElement('script');s.textContent='window._inj=1';document.body.appendChild(s);1",
    )
    wait(lambda: False, 0.3)
    check(
        run_js(pg, "typeof window._inj") == "undefined",
        f"dynamic {route}: injected inline script blocked (window._inj undefined)",
    )
    pg.deleteLater()

srv.shutdown()
shutil.rmtree(WORK, ignore_errors=True)

print(f"\n{'FAILED ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
sys.exit(1 if failures else 0)
