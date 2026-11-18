"""Tests for organization Cloudflare relay deploy, bundling, dialog hygiene, and CSP.

Run:  engine\\bin\\python-qgis.bat tests\\test_org_relay.py
"""
import contextlib
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))

SENTINEL = "cfat_SENTINEL_SECRET_TOKEN_9876543210"
SETTINGS_DIR = tempfile.mkdtemp(prefix="biruni-org-relay-")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = SETTINGS_DIR

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


# ---- 1. Fake Cloudflare API server -----------------------------------------------------------------
class FakeCloudflareState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.calls = []
        self.accounts = [{"id": "acc1", "name": "Survey Org"}]
        self.subdomain = "myorg"
        self.put_subdomain_fail = False
        self.scripts = []
        self.force_403 = False
        self.migration_error_once = False
        self.multipart_parts = []
        self.subdomain_post_body = None


STATE = FakeCloudflareState()


def parse_multipart(content_type, body_bytes):
    raw = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body_bytes
    msg = BytesParser(policy=email_policy).parsebytes(raw)
    parts = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        parts[name] = {
            "content_type": part.get_content_type(),
            "filename": part.get_filename(),
            "payload": part.get_payload(decode=True),
        }
    return parts


class FakeCfHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send_json(self, code, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        path = self.path.split("?", 1)[0]
        auth = self.headers.get("Authorization", "")
        n = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(n) if n > 0 else b""
        if path.startswith("/r/"):
            self.send_response(426)
            self.end_headers()
            return

        STATE.calls.append((method, path, auth))
        if STATE.force_403:
            self._send_json(403, {
                "success": False,
                "errors": [{"code": 10000, "message": f"Forbidden for token {SENTINEL}"}],
            })
            return

        if method == "GET" and path == "/client/v4/accounts":
            self._send_json(200, {"success": True, "result": STATE.accounts})
            return

        if method == "GET" and path.endswith("/workers/subdomain") and "/scripts/" not in path:
            if STATE.subdomain is None:
                self._send_json(404, {"success": False, "errors": [{"code": 10007, "message": "not found"}]})
            else:
                self._send_json(200, {"success": True, "result": {"subdomain": STATE.subdomain}})
            return

        if method == "PUT" and path.endswith("/workers/subdomain") and "/scripts/" not in path:
            if STATE.put_subdomain_fail:
                self._send_json(400, {"success": False, "errors": [{"code": 10063, "message": "onboarding needed"}]})
                return
            data = json.loads(body.decode("utf-8"))
            STATE.subdomain = data["subdomain"]
            self._send_json(200, {"success": True, "result": {"subdomain": STATE.subdomain}})
            return

        if method == "GET" and path.endswith("/workers/scripts"):
            self._send_json(200, {"success": True, "result": STATE.scripts})
            return

        if method == "PUT" and path.endswith("/workers/scripts/biruni-relay"):
            parts = parse_multipart(self.headers.get("Content-Type", ""), body)
            STATE.multipart_parts.append(parts)
            if STATE.migration_error_once:
                STATE.migration_error_once = False
                self._send_json(400, {
                    "success": False,
                    "errors": [{"code": 10021, "message": "Durable Object migration tag already applied"}],
                })
                return
            STATE.scripts = [{"id": "biruni-relay"}]
            self._send_json(200, {"success": True, "result": {"id": "biruni-relay"}})
            return

        if method == "POST" and path.endswith("/workers/scripts/biruni-relay/subdomain"):
            STATE.subdomain_post_body = json.loads(body.decode("utf-8"))
            self._send_json(200, {"success": True, "result": {"enabled": True}})
            return

        self._send_json(404, {"success": False, "errors": [{"message": "unknown route"}]})

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_PUT(self):  # noqa: N802
        self._handle("PUT")

    def do_POST(self):  # noqa: N802
        self._handle("POST")


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeCfHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["BIRUNI_CF_API"] = f"http://127.0.0.1:{srv.server_address[1]}/client/v4"

# ---- 2. Bundle check + node --check + no Qt in org_relay.py ----------------------------------------
from biruni.field_photos import bridge, org_relay  # noqa: E402
from biruni.secure import csp  # noqa: E402

with open(org_relay.__file__, "r", encoding="utf-8") as f:
    org_relay_src = f.read()
check("PyQt" not in org_relay_src and "qgis" not in org_relay_src.lower(),
      "org_relay.py does not import Qt or QGIS")

with open(os.path.join(ROOT, "relay", "hub.js"), "r", encoding="utf-8") as f:
    hub_src = f.read()
with open(os.path.join(ROOT, "relay", "worker.js"), "r", encoding="utf-8") as f:
    worker_src = f.read()
bundle_path = os.path.join(ROOT, "app", "plugins", "biruni", "field_photos", "org_relay_worker.js")
with open(bundle_path, "r", encoding="utf-8") as f:
    shipped_bundle = f.read()
fresh_bundle = org_relay.bundle(hub_src, worker_src)
check(shipped_bundle == fresh_bundle and "import " not in shipped_bundle and "export class Hub " not in shipped_bundle,
      "shipped org_relay_worker.js matches fresh org_relay.bundle()")

node = shutil.which("node") or next(
    (p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.isfile(p)), None
)
if node:
    p_chk = subprocess.run([node, "--check", bundle_path], capture_output=True, text=True, timeout=15)
    check(p_chk.returncode == 0, f"node --check org_relay_worker.js passes ({p_chk.stderr.strip()})")
else:
    check(False, "node is available for bundle check")

# ---- 3. ORG_RE validation --------------------------------------------------------------------------
for good_h in (
    "biruni-relay.myorg.workers.dev",
    "a.b.workers.dev",
    "r1-2.sub-3.workers.dev",
    ("a" * 63) + "." + ("b" * 63) + ".workers.dev",
):
    check(bool(bridge.ORG_RE.fullmatch(good_h)), f"ORG_RE accepts {good_h}")
for bad_h in (
    "",
    "workers.dev",
    "myorg.workers.dev",
    "a.b.c.workers.dev",
    "A.b.workers.dev",
    "a_b.c.workers.dev",
    "https://a.b.workers.dev",
    "a.b.workers.dev/r/",
    "a.b.workers.dev:443",
    ("a" * 64) + ".b.workers.dev",
    "a.b.workers.dev\n",
):
    check(not bridge.ORG_RE.fullmatch(bad_h), f"ORG_RE rejects {bad_h!r}")

# ---- 4. CSP: photos page has wss://*.workers.dev, d/ and s/ do not ---------------------------------
photos_csp = bridge.with_csp(bridge.phone_page({"en": {}}))
photos_conn = photos_csp.split("connect-src", 1)[1].split(";", 1)[0].split()
pol = csp.site_policies()
check("wss://*.workers.dev" in photos_conn, f"photos page CSP has wss://*.workers.dev ({photos_conn})")
check("wss://*.workers.dev" not in pol["d/index.html"]["connect"]
      and "wss://*.workers.dev" not in pol["s/index.html"]["connect"],
      "d/index.html and s/index.html CSP do not include wss://*.workers.dev")

# ---- 5. Captured stdout/stderr/logging around deploy runs ------------------------------------------
log_buf = io.StringIO()
log_handler = logging.StreamHandler(log_buf)
logging.getLogger().addHandler(log_handler)
out_buf = io.StringIO()
err_buf = io.StringIO()


def run_captured(fn):
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        return fn()


# 5a. Fresh deploy: call order, Authorization header, multipart metadata + bundle, subdomain POST
STATE.reset()
res1 = run_captured(lambda: org_relay.deploy(SENTINEL))
check(res1 == {"host": "biruni-relay.myorg.workers.dev", "account": "acc1"},
      f"fresh deploy returns expected host and account ({res1})")
expected_order = [
    ("GET", "/client/v4/accounts"),
    ("GET", "/client/v4/accounts/acc1/workers/subdomain"),
    ("GET", "/client/v4/accounts/acc1/workers/scripts"),
    ("PUT", "/client/v4/accounts/acc1/workers/scripts/biruni-relay"),
    ("POST", "/client/v4/accounts/acc1/workers/scripts/biruni-relay/subdomain"),
]
check([(m, p) for m, p, _a in STATE.calls] == expected_order,
      f"deploy call order matches spec ({[(m, p) for m, p, _a in STATE.calls]})")
check(all(a == f"Bearer {SENTINEL}" for _m, _p, a in STATE.calls),
      "every API call sends Authorization: Bearer <token>")

parts1 = STATE.multipart_parts[0]
meta1 = json.loads(parts1["metadata"]["payload"].decode("utf-8"))
check(meta1.get("main_module") == "worker.js"
      and meta1.get("compatibility_date") == "2026-01-01"
      and meta1.get("bindings") == [{"type": "durable_object_namespace", "name": "HUB", "class_name": "HubDO"}]
      and meta1.get("migrations") == {"new_tag": "v1", "new_sqlite_classes": ["HubDO"]}
      and "vars" not in meta1,
      f"fresh upload metadata fields match spec without vars ({meta1})")
check(parts1["worker.js"]["content_type"] == "application/javascript+module"
      and parts1["worker.js"]["payload"] == shipped_bundle.encode("utf-8"),
      "worker.js multipart part has application/javascript+module and exact bundle body")
check(STATE.subdomain_post_body == {"enabled": True, "previews_enabled": False},
      f"subdomain route enabled with previews_enabled=False ({STATE.subdomain_post_body})")

# 5b. Redeploy: omits migrations
STATE.multipart_parts.clear()
STATE.calls.clear()
res2 = run_captured(lambda: org_relay.deploy(SENTINEL))
meta2 = json.loads(STATE.multipart_parts[0]["metadata"]["payload"].decode("utf-8"))
check("migrations" not in meta2 and res2["host"] == "biruni-relay.myorg.workers.dev",
      f"redeploy omits migrations ({meta2})")

# 5c. Missing subdomain path: auto-creates biruni-xxxxxx; failure tells user to open Workers & Pages
STATE.reset()
STATE.subdomain = None
res3 = run_captured(lambda: org_relay.deploy(SENTINEL))
check(res3["host"].startswith("biruni-relay.biruni-") and res3["host"].endswith(".workers.dev")
      and any(m == "PUT" and p == "/client/v4/accounts/acc1/workers/subdomain" for m, p, _ in STATE.calls),
      f"missing subdomain creates biruni-<6> automatically ({res3['host']})")

STATE.reset()
STATE.subdomain = None
STATE.put_subdomain_fail = True
try:
    run_captured(lambda: org_relay.deploy(SENTINEL))
    check(False, "failed subdomain PUT should raise OrgRelayError")
except org_relay.OrgRelayError as e:
    check("Workers & Pages" in str(e) and SENTINEL not in str(e),
          f"failed subdomain PUT tells user to open Workers & Pages ({e})")

# 5d. Several accounts path
STATE.reset()
STATE.accounts = [{"id": "acc1", "name": "Org A"}, {"id": "acc2", "name": "Org B"}]
try:
    run_captured(lambda: org_relay.deploy(SENTINEL))
    check(False, "multiple accounts should raise ChooseAccount")
except org_relay.ChooseAccount as ca:
    check(len(ca.accounts) == 2 and ca.accounts[1]["id"] == "acc2",
          f"several accounts raises ChooseAccount ({ca.accounts})")
    res4 = run_captured(lambda: org_relay.deploy(SENTINEL, account_id=ca.accounts[1]["id"]))
    check(res4["account"] == "acc2" and res4["host"] == "biruni-relay.myorg.workers.dev",
          f"deploy with chosen account_id succeeds ({res4})")

# 5e. 403 scrubs token from OrgRelayError
STATE.reset()
STATE.force_403 = True
try:
    run_captured(lambda: org_relay.deploy(SENTINEL))
    check(False, "403 should raise OrgRelayError")
except org_relay.OrgRelayError as e:
    check(SENTINEL not in str(e) and "***" in str(e) and "Forbidden for token" in str(e),
          f"403 error carries scrubbed Cloudflare message without token ({e})")

# ---- 6. Dialog + QgsSettings + phone_url + sentinel leak scan -------------------------------------
from qgis.core import QgsApplication, QgsSettings  # noqa: E402
from biruni.field_photos.org_relay_dialog import OrgRelayDialog  # noqa: E402

qapp = QgsApplication([], True)
qapp.initQgis()

STATE.reset()
changed_hits = []
dlg = OrgRelayDialog(on_changed=lambda: changed_hits.append(bridge.org_host()))
dlg.token_edit.setText(SENTINEL)
run_captured(dlg.start_deploy)
check(dlg.token_edit.text() == "", "token field is cleared as soon as Deploy starts")

deadline = time.monotonic() + 10
while time.monotonic() < deadline and dlg._busy:
    qapp.processEvents()
    time.sleep(0.02)

check(bridge.org_host() == "biruni-relay.myorg.workers.dev" and changed_hits == ["biruni-relay.myorg.workers.dev"],
      f"dialog sets org_host and triggers QR refresh callback ({bridge.org_host()})")
k_test = bridge.new_key()
pu = bridge.phone_url(k_test, "en")
check(f"&p=2&o=biruni-relay.myorg.workers.dev" in pu, f"phone_url appends &o=<host> after &p=2 ({pu})")

dlg.stop_using()
check(bridge.org_host() == "" and changed_hits[-1] == "" and "dash.cloudflare.com" in dlg.status_lbl.text(),
      "Stop using it clears org_host and explains Worker can be deleted in dashboard")
dlg.close()

# Verify sentinel appears nowhere in QgsSettings, temp profile folder, stdout, stderr, or logging
st = QgsSettings()
st.sync()
all_settings = [f"{k}={st.value(k)}" for k in st.allKeys()]
leaked_files = []
for root, _dirs, names in os.walk(SETTINGS_DIR):
    for name in names:
        fp = os.path.join(root, name)
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            if SENTINEL in f.read():
                leaked_files.append(fp)

logging.getLogger().removeHandler(log_handler)
check(not any(SENTINEL in s for s in all_settings), "sentinel token is not in QgsSettings")
check(not leaked_files, f"sentinel token is not in temp profile folder ({leaked_files})")
check(SENTINEL not in out_buf.getvalue() and SENTINEL not in err_buf.getvalue()
      and SENTINEL not in log_buf.getvalue(),
      "sentinel token is not in captured stdout, stderr, or logging")

qapp.exitQgis()
srv.shutdown()
shutil.rmtree(SETTINGS_DIR, ignore_errors=True)
print("ALL PASS" if not failures else f"{len(failures)} FAILED", flush=True)
sys.exit(1 if failures else 0)
