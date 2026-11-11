r"""Updates of Biruni's own code: only the files that changed are downloaded, the list is signed, the new copy
runs from the next start in the user's folder, a copy that does not start well is left for the one before, and
installing a newer Biruni wins over any update.

All on this PC: an installed copy of Biruni and a newer one in temporary folders, the published files served by
a small web server here, a test key.

Run:  engine\bin\python-qgis.bat tests\test_update.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))
work = tempfile.mkdtemp()
UPDATES = os.path.join(work, "updates")
os.environ["BIRUNI_UPDATES"] = UPDATES

from biruni.secure import crypto  # noqa: E402
from biruni.update import boot, client  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


def set_version(folder, version, key=None):
    p = os.path.join(folder, "version.py")
    s = open(p, encoding="utf-8").read()
    s = re.sub(r'^VERSION = ".*"$', f'VERSION = "{version}"', s, flags=re.M)
    if key:
        s = re.sub(r'^UPDATE_KEY = ".*"$', f'UPDATE_KEY = "{key}"', s, flags=re.M)
    open(p, "w", encoding="utf-8", newline="\n").write(s)


# ---- an installed Biruni 0.2.0, and a 0.2.1 with two files changed and one new -----------------------------------
seed = os.urandom(32)
pub = crypto.public_key(seed).hex()
key_file = os.path.join(work, "key.txt")
open(key_file, "w").write(seed.hex())
src = os.path.join(ROOT, "app", "plugins", "biruni")
ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
installed = os.path.join(work, "installed", "biruni")
shutil.copytree(src, installed, ignore=ignore)
set_version(installed, "0.2.0", pub)
newer = os.path.join(work, "newer", "biruni")
shutil.copytree(installed, newer, ignore=ignore)
set_version(newer, "0.2.1")
with open(os.path.join(newer, "update_marker.py"), "w", encoding="utf-8") as f:
    f.write('WHO = "the update"\n')
with open(os.path.join(newer, "about.py"), "a", encoding="utf-8") as f:
    f.write("\n# changed by the update\n")
site = os.path.join(work, "site")


def publish(source, *extra):
    return subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "publish_update.py"), site, "--key", key_file,
                           "--source", source, "--notes-ar", "إصلاحات", "--notes-en", "Fixes", *extra],
                          capture_output=True, text=True, encoding="utf-8")


r = publish(newer)
check(r.returncode == 0 and "version 0.2.1" in r.stdout, "the update is published: " + (r.stdout + r.stderr).strip()[-160:])
m_file = os.path.join(site, "updates", "manifest.json")
man = json.load(open(m_file, encoding="utf-8"))
blobs = sum(len(f) for _d, _s, f in os.walk(os.path.join(site, "updates", "files")))
check(man["version"] == "0.2.1" and len(man["files"]) == blobs and man["notes"]["ar"] == "إصلاحات",
      f"the list: {len(man['files'])} files, each stored once by its fingerprint")
check(publish(newer).returncode != 0, "the same version is not published twice (bump the version first)")
wrong = os.path.join(work, "wrong.txt")
open(wrong, "w").write(os.urandom(32).hex())
r2 = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "publish_update.py"), site, "--key", wrong,
                     "--source", newer], capture_output=True, text=True)
check(r2.returncode != 0 and "UPDATE_KEY" in (r2.stdout + r2.stderr), "a key that is not Biruni's is refused when publishing")


# ---- the web server: what is asked of it is counted ----------------------------------------------------------------
asked = []


class Serve(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=site, **k)

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        asked.append(self.path)
        super().do_GET()


httpd = ThreadingHTTPServer(("127.0.0.1", 0), Serve)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{httpd.server_address[1]}/updates/"

# ---- checking and staging -----------------------------------------------------------------------------------------
check(client.check(URL, pub, "0.2.1") is None, "a Biruni already at that version is told there is nothing new")
m = client.check(URL, pub, "0.2.0")
check(m is not None and m["version"] == "0.2.1", "a Biruni at 0.2.0 sees 0.2.1")
try:
    client.check(URL, os.urandom(32).hex(), "0.2.0")
    check(False, "a list checked with another key is refused")
except client.UpdateError:
    check(True, "a list checked with another key is refused")
asked.clear()
res = client.stage(m, URL, installed)
got = [p for p in asked if "/files/" in p]
check(res["downloaded"] == 3 and len(got) == 3 and res["copied"] == len(m["files"]) - 3,
      f"only what changed is downloaded: {res['downloaded']} files, {res['bytes']} bytes; {res['copied']} copied from this computer")
copy = os.path.join(UPDATES, "0.2.1", "biruni")
check(os.path.isfile(os.path.join(copy, "update_marker.py")) and boot.folder_version(copy) == "0.2.1"
      and boot.read_state(UPDATES).get("next") == "0.2.1", "the new copy is in the user's folder, set for the next start")
check(boot.folder_version(installed) == "0.2.0" and not os.path.exists(os.path.join(installed, "update_marker.py")),
      "the installed program is not touched")

PROBE = r"""
import json, os, sys
sys.path.insert(0, {plugins!r}); sys.path.insert(1, {python!r})
import biruni, biruni.version
mk = None
try:
    import biruni.update_marker as u; mk = u.WHO
except ImportError:
    pass
if {healthy}:
    from biruni.update import boot; boot.healthy()
print(json.dumps({{"version": biruni.version.VERSION, "from": biruni.version.__file__, "marker": mk}}))
"""


def start(healthy=False):
    """Biruni starts: which copy runs?"""
    code = PROBE.format(plugins=os.path.dirname(installed), python=os.path.join(ROOT, "app", "python"), healthy=healthy)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=dict(os.environ))
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": r.stderr[-300:]}


s1 = start()
check(s1.get("version") == "0.2.1" and s1.get("marker") == "the update" and UPDATES in (s1.get("from") or ""),
      f"the next start runs the update, every module from the user's folder ({s1})")
st = boot.read_state(UPDATES)
check(st.get("current") == "0.2.1" and st.get("trial") is True and not st.get("next"), "… on trial")
s2 = start()
st = boot.read_state(UPDATES)
check(s2.get("version") == "0.2.0" and "0.2.1" in st.get("bad", []),
      f"it never said it started well: the start after runs the installed copy again, 0.2.1 is set aside ({s2.get('version')})")
check(client.is_bad("0.2.1"), "… and the automatic check does not offer it again")
client.stage(m, URL, installed)
s3 = start(healthy=True)
s4 = start()
st = boot.read_state(UPDATES)
check(s3.get("version") == "0.2.1" and s4.get("version") == "0.2.1" and st.get("trial") is False and "0.2.1" not in st.get("bad"),
      "staged again and started well: it stays")

# ---- a damaged list, a damaged file, a bad name --------------------------------------------------------------------
good = open(m_file, "rb").read()
open(m_file, "wb").write(good.replace(b'"0.2.1"', b'"0.2.9"'))
try:
    client.check(URL, pub, "0.2.0")
    check(False, "a list changed after signing is refused")
except client.UpdateError:
    check(True, "a list changed after signing is refused")
open(m_file, "wb").write(good)
sha = man["files"]["update_marker.py"][0]
blob = os.path.join(site, "updates", "files", sha[:2], sha)
okb = open(blob, "rb").read()
open(blob, "wb").write(b'WHO = "someone else"\n')
shutil.rmtree(os.path.join(UPDATES, "0.2.1"))
st = boot.read_state(UPDATES)
st.update(current=None, previous=None, next=None)
boot.write_state(UPDATES, st)
try:
    client.stage(m, URL, installed)
    check(False, "a file changed on the server is refused")
except client.UpdateError as e:
    check("update_marker.py" in str(e) and not os.path.isdir(os.path.join(UPDATES, "0.2.1"))
          and not boot.read_state(UPDATES).get("next"), f"a file changed on the server is refused, nothing is staged ({e})")
open(blob, "wb").write(okb)
try:
    client.plan({"files": {"../../evil.py": ["0" * 64, 1]}}, installed)
    check(False, "a file name that leaves the folder is refused")
except client.UpdateError:
    check(True, "a file name that leaves the folder is refused")

# ---- installing a newer Biruni wins ----------------------------------------------------------------------------------
client.stage(m, URL, installed)
start(healthy=True)
set_version(installed, "0.3.0")
s5 = start()
check(s5.get("version") == "0.3.0" and UPDATES not in (s5.get("from") or ""), "a newer Biruni installed over it runs, not the older update")
set_version(installed, "0.2.0")

# ---- not an installed Biruni: the code runs as it is -----------------------------------------------------------------
env = {k: v for k, v in os.environ.items() if k not in ("BIRUNI_UPDATES", "BIRUNI_HOME")}
r = subprocess.run([sys.executable, "-c", PROBE.format(plugins=os.path.dirname(installed),
                    python=os.path.join(ROOT, "app", "python"), healthy=False)], capture_output=True, text=True, env=env)
out = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}
check(installed in (out.get("from") or "") and out.get("marker") is None,
      "outside an installed Biruni (tests, development) no update is used " + (r.stdout + r.stderr).strip()[-200:])

httpd.shutdown()
print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}", flush=True)
os._exit(1 if failures else 0)
