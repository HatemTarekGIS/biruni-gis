r"""No-loss P9 stage 9c: Syncthing between office PCs (no network, no real binary).

Tests:
- gui_bound_to_loopback_with_password (parse the generated config.xml);
- office_sendonly_center_receiveonly_versioned;
- peer_100_never_marks_backup;
- secrets_never_in_argv.

Run:  engine\bin\python-qgis.bat tests\test_backup_syncthing.py
"""
import json
import os
import shutil
import sys
import xml.etree.ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backup_helpers import PY_ENV, Checker, Env, other_volume  # noqa: E402
from biruni.backup import engine, health_rows, syncthing  # noqa: E402
from biruni.field_photos.tunnel import unprotect  # noqa: E402
from biruni.health import Ctx  # noqa: E402

check = Checker()
other_volume(True)
DAY = 86400.0
PEER = "ABCDEF1-2345678-ABCDEF1-2345678-ABCDEF1-2345678-ABCDEF1-2345678"

FAKE_ST = """import json, os, sys
args = sys.argv[1:]
home = args[args.index("--home") + 1] if "--home" in args else ""
if home:
    os.makedirs(home, exist_ok=True)
    open(os.path.join(home, "argv.log"), "a", encoding="utf-8").write(json.dumps(args) + "\\n")
if "--device-id" in args:
    sys.stdout.write("MYDEV11-2345678-MYDEV11-2345678-MYDEV11-2345678-MYDEV11-2345678\\n")
    sys.exit(0)
if args and args[0] == "completion":
    sys.stdout.write(json.dumps({"completion": 100.0, "connected": True}) + "\\n")
    sys.exit(0)
sys.exit(0)
"""


def make_fake(work):
    p = os.path.join(work, "fake_syncthing.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write(FAKE_ST)
    return [sys.executable, p]


# ---- gui_bound_to_loopback_with_password -----------------------------------------------------------------------------------
e = Env()
home = os.path.join(e.work, "st-home")
st = syncthing.Syncthing(make_fake(e.work), home=home, cert_folder=e.cert, lan_only=True, env=PY_ENV)
info = st.ensure_gui_lock()
cfg_path = os.path.join(home, "config.xml")
root = ET.parse(cfg_path).getroot()
gui = root.find("gui")
addr = (gui.findtext("address") or "").strip()
user = (gui.findtext("user") or "").strip()
pw_hash = (gui.findtext("password") or "").strip()
api_key = (gui.findtext("apikey") or "").strip()
dp_user, dp_pw = syncthing.load_gui_credentials(e.cert, home)
dp_api = syncthing.load_api_key(e.cert, home)

check(addr.startswith("127.0.0.1:") and int(addr.split(":", 1)[1]) > 0,
      f"gui_bound_to_loopback_with_password: GUI address is loopback ({addr})")
check(bool(user) and user == dp_user and len(dp_pw) >= 20 and pw_hash != dp_pw and pw_hash.startswith("$2"),
      "GUI has a random user and bcrypt-hashed password (plaintext password is not in config.xml)")
check(bool(api_key) and api_key == dp_api and len(api_key) >= 20,
      "API key is generated and stored under DPAPI")
raw_api_file = open(os.path.join(e.cert, syncthing.API_KEY_FILE), encoding="ascii").read().strip()
raw_gui_file = open(os.path.join(e.cert, syncthing.GUI_PW_FILE), encoding="ascii").read().strip()
check(api_key not in raw_api_file and unprotect(raw_api_file) == api_key
      and dp_pw not in raw_gui_file and unprotect(raw_gui_file) == f"{dp_user}:{dp_pw}",
      "DPAPI files on disk are protected, not plaintext")
opts = root.find("options")
check(opts.findtext("globalAnnounceEnabled") == "false" and opts.findtext("relaysEnabled") == "false"
      and opts.findtext("natEnabled") == "false" and opts.findtext("autoUpgradeIntervalH") == "0",
      "LAN-only mode disables global discovery, relays and NAT; auto-upgrade is off")

# ---- office_sendonly_center_receiveonly_versioned --------------------------------------------------------------------------
st.share(e.photos, PEER, role="office")
root = ET.parse(cfg_path).getroot()
fl_office = root.find("folder")
check(fl_office is not None and fl_office.attrib.get("type") == "sendonly"
      and fl_office.find("versioning") is None
      and any(d.attrib.get("id") == PEER for d in fl_office.findall("device")),
      "office_sendonly_center_receiveonly_versioned: office/branch role configures sendonly folder")

st.share(e.photos, PEER, role="center")
root = ET.parse(cfg_path).getroot()
fl_center = root.find("folder")
ver = fl_center.find("versioning") if fl_center is not None else None
params = {p.attrib.get("key"): p.attrib.get("val") for p in (ver.findall("param") if ver is not None else [])}
check(fl_center is not None and fl_center.attrib.get("type") == "receiveonly"
      and ver is not None and ver.attrib.get("type") == "staggered"
      and params.get("maxAge") == "31536000",
      f"centre role configures receiveonly with staggered versioning ({params})")

# ---- peer_100_never_marks_backup -------------------------------------------------------------------------------------------
items = e.add_photos(3)
now = 1_764_000_000.0
with open(os.path.join(home, "mock_completion.json"), "w", encoding="utf-8") as f:
    json.dump({PEER: {"completion": 100.0, "connected": True, "name": "Centre PC"}}, f)
comp = st.completion(PEER, now=now)
check(comp["completion"] == 100.0, "peer completion reports 100%")
check(not any(e.places(i).get("backup") for i, _p, _s in items),
      "peer_100_never_marks_backup: a peer at 100% NEVER marks `backup` in the CopyLedger")

# Check health rows for sync:<peer>: 100% -> ok, below 100% for >24h -> warn, unseen for >=7d -> bad
ctx = Ctx(get_setting=e.settings, cert_folder=e.cert, photos_folder=e.photos, now=now)
rows = {r["id"]: r for r in health_rows._check_backup(ctx)}
check(rows.get(f"sync:{PEER}", {}).get("level") == "ok", "health row is green when peer is at 100% and seen recently")

with open(os.path.join(home, "mock_completion.json"), "w", encoding="utf-8") as f:
    json.dump({PEER: {"completion": 65.0, "connected": True, "name": "Centre PC"}}, f)
st.completion(PEER, now=now)
st.completion(PEER, now=now + 25 * 3600)
ctx_25h = Ctx(get_setting=e.settings, cert_folder=e.cert, photos_folder=e.photos, now=now + 25 * 3600)
rows_25h = {r["id"]: r for r in health_rows._check_backup(ctx_25h)}
check(rows_25h.get(f"sync:{PEER}", {}).get("level") == "warn",
      f"health row is amber when peer stays below 100% for 24 h ({rows_25h.get(f'sync:{PEER}')})")

ctx_8d = Ctx(get_setting=e.settings, cert_folder=e.cert, photos_folder=e.photos, now=now + 9 * DAY)
rows_8d = {r["id"]: r for r in health_rows._check_backup(ctx_8d)}
check(rows_8d.get(f"sync:{PEER}", {}).get("level") == "bad",
      f"health row is red when peer has been unseen for 7 days ({rows_8d.get(f'sync:{PEER}')})")

# ---- secrets_never_in_argv -------------------------------------------------------------------------------------------------
serve_cmd = st.serve_argv()
did = st.device_id()
p = st.start()
p.wait(timeout=10)
argv_log = open(os.path.join(home, "argv.log"), encoding="utf-8").read()
combined = " ".join(serve_cmd) + "\n" + argv_log + "\n" + open(os.path.join(e.cert, "backup", "state.json"), encoding="utf-8").read()
check(did.startswith("MYDEV11-"), f"device_id() queries the child process ({did})")
check("--no-browser" in serve_cmd and "--no-upgrade" in serve_cmd and "127.0.0.1:" in " ".join(serve_cmd),
      "serve command runs with --no-browser --no-upgrade and loopback --gui-address")
check(dp_pw not in combined and dp_api not in combined and pw_hash not in combined,
      "secrets_never_in_argv: GUI password, bcrypt hash and API key never appear in argv or state.json")

shutil.rmtree(e.work, ignore_errors=True)
check.done()
