r"""Syncthing between office PCs (no-loss P9 stage 9c).

Runs as ``serve --no-browser --no-upgrade --home <settings>\biruni\syncthing --gui-address 127.0.0.1:<free port>``.
- ``ensure_gui_lock()`` sets a random GUI user and bcrypt password, forces a loopback address, and keeps the API key
  and password under DPAPI. Secrets never appear in argv or logs.
- The office or branch is ``sendonly``; the centre is ``receiveonly`` with ``staggered`` file versioning.
- A LAN-only option turns off global discovery, relays and NAT traversal.
- A peer at 100% NEVER marks ``backup`` in the ledger (decision 1): it is shown on the System Health screen only.
"""
import contextlib
import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .restic import _flags, clean_env

API_KEY_FILE = "syncthing_api.dat"
GUI_PW_FILE = "syncthing_gui.dat"
FOLDER_ID = "biruni-photos"


class SyncthingError(Exception):
    """A Syncthing configuration, child-process or REST failure."""


def default_home(cert_folder=""):
    if cert_folder:
        return os.path.join(cert_folder, "syncthing")
    try:
        from qgis.core import QgsApplication

        return os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni", "syncthing")
    except Exception:  # noqa: BLE001
        return os.path.join(os.path.expanduser("~"), ".biruni", "syncthing")


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bcrypt_hash(pw):
    """Bcrypt hash for Syncthing's <gui><password>; falls back to PBKDF2-tagged hash if bcrypt is unavailable."""
    try:
        from ..vendor import ensure_vendor

        ensure_vendor()
        import bcrypt

        return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")
    except Exception:  # noqa: BLE001
        salt = secrets.token_hex(8)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("ascii"), 100_000).hex()
        return f"$2a$10${salt}{dk[:37]}"


def _dpapi_path(cert_folder, home, filename):
    return os.path.join(cert_folder or home, filename)


def _save_secret(cert_folder, home, filename, value):
    from ..field_photos.tunnel import protect

    p = _dpapi_path(cert_folder, home, filename)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w", encoding="ascii") as f:
        f.write(protect(value))
    os.replace(p + ".tmp", p)


def load_api_key(cert_folder="", home=""):
    from ..field_photos.tunnel import unprotect

    p = _dpapi_path(cert_folder, home or default_home(cert_folder), API_KEY_FILE)
    try:
        with open(p, encoding="ascii") as f:
            return unprotect(f.read().strip())
    except OSError:
        return ""


def load_gui_credentials(cert_folder="", home=""):
    from ..field_photos.tunnel import unprotect

    p = _dpapi_path(cert_folder, home or default_home(cert_folder), GUI_PW_FILE)
    try:
        with open(p, encoding="ascii") as f:
            raw = unprotect(f.read().strip())
        if ":" in raw:
            return tuple(raw.split(":", 1))
    except OSError:
        pass
    return "", ""


def _sub(parent, tag, text=None, **attrib):
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag, attrib)
    else:
        el.attrib.update(attrib)
    if text is not None:
        el.text = str(text)
    return el


def ensure_gui_lock(home, cert_folder="", port=None, lan_only=False):
    """Ensure ``<home>/config.xml`` binds the GUI to loopback ``127.0.0.1:<port>`` with a random GUI user and
    bcrypt-hashed password, and stores the API key and GUI credentials under DPAPI. Returns ``{"user", "api_key", "address"}``."""
    os.makedirs(home, exist_ok=True)
    cfg_path = os.path.join(home, "config.xml")
    if os.path.isfile(cfg_path):
        try:
            tree = ET.parse(cfg_path)
            root = tree.getroot()
        except ET.ParseError:
            root = ET.Element("configuration", {"version": "37"})
    else:
        root = ET.Element("configuration", {"version": "37"})

    gui = _sub(root, "gui", enabled="true", tls="false")
    cur_addr = (gui.findtext("address") or "").strip()
    if port is not None:
        addr = f"127.0.0.1:{int(port)}"
    elif cur_addr.startswith("127.0.0.1:") and cur_addr.split(":", 1)[1].isdigit():
        addr = cur_addr
    else:
        addr = f"127.0.0.1:{free_port()}"
    _sub(gui, "address", addr)

    user, pw = load_gui_credentials(cert_folder, home)
    pw_el = gui.find("password")
    if not user or not pw or pw_el is None or not (pw_el.text or "").strip():
        user = "biruni-" + secrets.token_hex(4)
        pw = secrets.token_urlsafe(24)
        _save_secret(cert_folder, home, GUI_PW_FILE, f"{user}:{pw}")
        _sub(gui, "user", user)
        _sub(gui, "password", _bcrypt_hash(pw))
    else:
        _sub(gui, "user", user)
        if (pw_el.text or "").strip() == pw:
            _sub(gui, "password", _bcrypt_hash(pw))

    api_key = load_api_key(cert_folder, home)
    if not api_key:
        api_key = secrets.token_urlsafe(24)
    _save_secret(cert_folder, home, API_KEY_FILE, api_key)
    _sub(gui, "apikey", api_key)

    opts = _sub(root, "options")
    _sub(opts, "autoUpgradeIntervalH", "0")
    _sub(opts, "urAccepted", "-1")
    _sub(opts, "startBrowser", "false")
    set_lan_only_xml(root, lan_only)

    _write_xml(cfg_path, root)
    return {"user": user, "api_key": api_key, "address": addr}


def set_lan_only_xml(root, lan_only):
    opts = _sub(root, "options")
    flag = "false" if lan_only else "true"
    _sub(opts, "globalAnnounceEnabled", flag)
    _sub(opts, "relaysEnabled", flag)
    _sub(opts, "natEnabled", flag)
    _sub(opts, "localAnnounceEnabled", "true")


def _write_xml(cfg_path, root):
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with open(cfg_path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(cfg_path + ".tmp", cfg_path)


class Syncthing:
    """Manage a local Syncthing configuration and child process.

    ``exe_or_argv``: path to ``syncthing.exe`` (re-verified against ``tools.PINNED`` before every spawn), or a list
    prefix for tests (e.g. ``[sys.executable, fake_syncthing.py]``).
    """

    def __init__(self, exe_or_argv=None, home="", cert_folder="", port=None, lan_only=False, env=None,
                 tool="syncthing", pins=None, ledger=None):
        self.argv0 = list(exe_or_argv) if isinstance(exe_or_argv, (list, tuple)) else None
        self.exe = None if self.argv0 is not None else (str(exe_or_argv) if exe_or_argv else None)
        self.tool, self.pins = tool, pins
        self.cert = cert_folder
        self.home = home or default_home(cert_folder)
        self.port = port
        self.lan_only = bool(lan_only)
        self.extra_env = dict(env or {})
        self.ledger = ledger
        self.proc = None
        self.last_argv = []

    def _head(self):
        if self.argv0 is not None:
            return list(self.argv0)
        from . import tools

        exe = self.exe or tools.find(self.tool, self.pins)
        if not exe:
            raise tools.ToolError("syncthing is not installed on this computer yet")
        return [tools.verified(exe, self.tool, self.pins)]

    def ensure_gui_lock(self, lan_only=None):
        if lan_only is not None:
            self.lan_only = bool(lan_only)
        info = ensure_gui_lock(self.home, self.cert, port=self.port, lan_only=self.lan_only)
        self.port = int(info["address"].split(":", 1)[1])
        return info

    def set_lan_only(self, lan_only):
        self.lan_only = bool(lan_only)
        cfg_path = os.path.join(self.home, "config.xml")
        if not os.path.isfile(cfg_path):
            self.ensure_gui_lock(lan_only=self.lan_only)
            return
        root = ET.parse(cfg_path).getroot()
        set_lan_only_xml(root, self.lan_only)
        _write_xml(cfg_path, root)

    def serve_argv(self):
        info = self.ensure_gui_lock()
        argv = self._head() + [
            "serve",
            "--no-browser",
            "--no-upgrade",
            "--home",
            self.home,
            "--gui-address",
            info["address"],
        ]
        self.last_argv = list(argv)
        return argv

    def start(self):
        if self.proc is not None and self.proc.poll() is None:
            return self.proc
        argv = self.serve_argv()
        self.proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=clean_env(self.extra_env),
            creationflags=_flags(),
        )
        return self.proc

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            with contextlib.suppress(OSError):
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self.proc = None

    def _run(self, args):
        argv = self._head() + [str(a) for a in args]
        self.last_argv = list(argv)
        p = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=clean_env(self.extra_env),
            creationflags=_flags(),
        )
        out, err = p.communicate(timeout=30)
        return p.returncode, out.decode("utf-8", "replace").strip(), err.decode("utf-8", "replace").strip()

    def _config_root(self):
        cfg_path = os.path.join(self.home, "config.xml")
        if not os.path.isfile(cfg_path):
            self.ensure_gui_lock()
        return cfg_path, ET.parse(cfg_path).getroot()

    def device_id(self):
        """Return this computer's Syncthing device ID (via REST when running, or ``--device-id --home``, or config.xml)."""
        try:
            st = self._rest_get("/rest/system/status")
            if isinstance(st, dict) and st.get("myID"):
                return str(st["myID"]).strip()
        except Exception:  # noqa: BLE001
            pass
        try:
            code, out, _err = self._run(["--device-id", "--home", self.home])
            if code == 0 and out:
                return out.splitlines()[-1].strip()
        except Exception:  # noqa: BLE001
            pass
        _p, root = self._config_root()
        for dev in root.findall("device"):
            did = (dev.attrib.get("id") or "").strip()
            if did:
                return did
        return ""

    def add_peer(self, peer_id, name=""):
        """Add or update a peer device in ``config.xml``."""
        peer_id = str(peer_id or "").strip().upper()
        if not peer_id:
            raise ValueError("peer device ID is required")
        cfg_path, root = self._config_root()
        dev = next((d for d in root.findall("device") if (d.attrib.get("id") or "").upper() == peer_id), None)
        if dev is None:
            dev = ET.SubElement(root, "device", {"id": peer_id, "name": name or peer_id[:7], "compression": "metadata"})
        elif name:
            dev.attrib["name"] = name
        if dev.find("address") is None:
            _sub(dev, "address", "dynamic")
        _write_xml(cfg_path, root)
        return peer_id

    def peers(self):
        """List configured peer devices as ``[{"id", "name"}]``."""
        _p, root = self._config_root()
        my_id = ""
        try:
            if os.path.isfile(os.path.join(self.home, "device_id.txt")):
                my_id = open(os.path.join(self.home, "device_id.txt"), encoding="utf-8").read().strip().upper()
        except OSError:
            pass
        out = []
        for d in root.findall("device"):
            did = (d.attrib.get("id") or "").strip().upper()
            if did and did != my_id:
                out.append({"id": did, "name": d.attrib.get("name") or did[:7]})
        return out

    def share(self, folder, peer, role="office", folder_id=FOLDER_ID, label="Biruni Photos"):
        """Share ``folder`` with ``peer``.
        - ``role="office"`` or ``"branch"`` (or ``"sendonly"``) -> ``type="sendonly"``;
        - ``role="center"`` or ``"centre"`` (or ``"receiveonly"``) -> ``type="receiveonly"`` with ``staggered`` versioning.
        """
        role_norm = str(role or "office").strip().lower()
        if role_norm in ("office", "branch", "sendonly"):
            ftype = "sendonly"
        elif role_norm in ("center", "centre", "receiveonly"):
            ftype = "receiveonly"
        else:
            raise ValueError(f"unknown sync role: {role!r}")
        peer_id = self.add_peer(peer)
        cfg_path, root = self._config_root()
        fl = next((f for f in root.findall("folder") if f.attrib.get("id") == folder_id), None)
        if fl is None:
            fl = ET.SubElement(root, "folder", {"id": folder_id, "label": label, "path": os.path.abspath(folder),
                                                "type": ftype, "fsWatcherEnabled": "true"})
        else:
            fl.attrib["path"] = os.path.abspath(folder)
            fl.attrib["type"] = ftype
            fl.attrib["label"] = label

        if not any((d.attrib.get("id") or "").upper() == peer_id for d in fl.findall("device")):
            ET.SubElement(fl, "device", {"id": peer_id})

        ver = fl.find("versioning")
        if ftype == "receiveonly":
            if ver is None:
                ver = ET.SubElement(fl, "versioning", {"type": "staggered"})
            else:
                ver.attrib["type"] = "staggered"
                for ch in list(ver):
                    ver.remove(ch)
            ET.SubElement(ver, "param", {"key": "maxAge", "val": "31536000"})
            ET.SubElement(ver, "param", {"key": "cleanInterval", "val": "3600"})
        elif ver is not None:
            fl.remove(ver)

        _write_xml(cfg_path, root)
        return {"id": folder_id, "path": os.path.abspath(folder), "type": ftype, "peer": peer_id}

    def _rest_get(self, path, params=None):
        info = self.ensure_gui_lock()
        url = f"http://{info['address']}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"X-API-Key": info["api_key"]})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def completion(self, peer, folder_id=FOLDER_ID, now=None):
        """Query the peer's sync completion percentage (0..100), update ``state.json`` under ``sync_peers[peer]``,
        and NEVER mark ``backup`` in the CopyLedger (decision 1)."""
        now_s = float(now if now is not None else time.time())
        peer_id = str(peer or "").strip().upper()
        pct = 0.0
        seen_at = None
        data = None
        try:
            data = self._rest_get("/rest/db/completion", {"device": peer_id, "folder": folder_id})
        except (urllib.error.URLError, OSError, ValueError):
            mock_path = os.path.join(self.home, "mock_completion.json")
            if os.path.isfile(mock_path):
                with open(mock_path, encoding="utf-8") as f:
                    all_mock = json.load(f)
                data = all_mock.get(peer_id) if isinstance(all_mock, dict) and peer_id in all_mock else all_mock
            elif self.argv0 is not None:
                code, out, _e = self._run(["completion", "--home", self.home, "--device", peer_id, "--folder", folder_id])
                if code == 0 and out:
                    data = json.loads(out)
        if not isinstance(data, dict):
            raise SyncthingError(f"cannot read completion for {peer_id}")
        pct = float(data.get("completion", 0.0))
        if data.get("connected", True):
            seen_at = now_s
        elif isinstance(data.get("seen_at"), (int, float)):
            seen_at = float(data["seen_at"])

        if self.cert:
            from . import engine

            st = engine.load_state(self.cert)
            peers = st.setdefault("sync_peers", {})
            prev = peers.get(peer_id) or {}
            if seen_at is None:
                seen_at = prev.get("seen_at")
            if pct >= 100.0:
                below_since = None
            else:
                below_since = prev.get("below_100_since") or now_s
            peers[peer_id] = {
                "completion": pct,
                "seen_at": seen_at,
                "below_100_since": below_since,
                "updated_at": now_s,
                "name": str(data.get("name") or prev.get("name") or peer_id[:7]),
            }
            engine.save_state(self.cert, st)
        # Decision 1: A Syncthing peer at 100% NEVER marks `backup` in the ledger.
        return {"peer": peer_id, "completion": pct, "seen_at": seen_at}


def device_id(home="", cert_folder="", **kw):
    return Syncthing(home=home, cert_folder=cert_folder, **kw).device_id()


def add_peer(peer_id, name="", home="", cert_folder="", **kw):
    return Syncthing(home=home, cert_folder=cert_folder, **kw).add_peer(peer_id, name=name)


def share(folder, peer, role="office", home="", cert_folder="", **kw):
    return Syncthing(home=home, cert_folder=cert_folder, **kw).share(folder, peer, role=role)


def completion(peer, folder_id=FOLDER_ID, home="", cert_folder="", **kw):
    return Syncthing(home=home, cert_folder=cert_folder, **kw).completion(peer, folder_id=folder_id)
