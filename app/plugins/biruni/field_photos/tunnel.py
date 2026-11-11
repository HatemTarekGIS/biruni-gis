""""From anywhere": a temporary public HTTPS address for the photo server, without an
account or any setting.

It uses Cloudflare's free quick tunnels (cloudflared, Apache-2.0). The program is
downloaded once from Cloudflare's GitHub releases, its Cloudflare code signature is
checked, and it runs hidden. The tunnel only passes the bytes through - nothing is kept
there - and its address (a random name under trycloudflare.com) dies with it. The
phone also gets a real certificate this way, so it shows no "not private" warning.

The process is put in a Windows job object that ends it when Biruni ends, even when
Biruni is killed or the power goes.

A user who wants a fixed address can use their own Cloudflare tunnel instead: they make it
once in their Cloudflare account (their domain) and paste its token; Biruni runs it the
same way. The token is kept encrypted for this Windows user (DPAPI).
"""
import base64
import binascii
import json
import os
import queue
import re
import subprocess
import sys
import threading
import urllib.request

DOWNLOAD = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
EXE = "cloudflared.exe"
URL_RE = re.compile(r"https://[-a-z0-9]+\.trycloudflare\.com")
READY_RE = re.compile(r"Registered tunnel connection|Connection [0-9a-f-]+ registered", re.I)
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9+/=_-]{40,}")
FAIL_RE = re.compile(r"Unauthorized|Invalid tunnel secret|not valid|token.*invalid|failed to request quick Tunnel|"
                     r"failed to unmarshal quick Tunnel", re.I)


def token_from(text):
    """The tunnel token in what the user pasted (the token, or the whole install command), or None."""
    for m in TOKEN_RE.finditer(text or ""):
        t = m.group(0)
        try:
            d = json.loads(base64.b64decode(t + "=" * (-len(t) % 4), altchars=b"-_" if "-" in t or "_" in t else None))
        except (ValueError, binascii.Error):
            continue
        if isinstance(d, dict) and {"a", "t", "s"} <= set(d):
            return t
    return None


def host_from(text):
    """photos.example.com from what the user typed (with or without https:// and a path), or None."""
    h = re.sub(r"^[a-z]+://", "", (text or "").strip(), flags=re.I).split("/")[0].split(":")[0].strip(".").lower()
    return h if re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", h) else None


def protect(text):
    """Text kept encrypted for this Windows user (DPAPI); stored as base64."""
    if not text:
        return ""
    data = text.encode("utf-8")
    if sys.platform == "win32":
        data = _dpapi(data, True)
    return base64.b64encode(data).decode("ascii")


def unprotect(stored):
    if not stored:
        return ""
    try:
        data = base64.b64decode(stored)
        if sys.platform == "win32":
            data = _dpapi(data, False)
        return data.decode("utf-8")
    except (ValueError, OSError, UnicodeDecodeError):
        return ""


def _dpapi(data, encrypt):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    crypt = ctypes.windll.crypt32
    fn = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):  # UI_FORBIDDEN
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def tools_folder():
    from qgis.core import QgsApplication

    return os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni", "tools")


def find_exe(folder=None):
    """cloudflared shipped with Biruni (app/tools) or downloaded before, else None."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.normpath(os.path.join(here, "..", "..", "..", "tools", EXE)),
                 os.path.join(folder or tools_folder(), EXE)):
        if os.path.isfile(path):
            return path
    return None


def signed_by_cloudflare(path):
    """True when Windows says the file carries a valid Cloudflare code signature."""
    if sys.platform != "win32":
        return True
    ps = ("$s=Get-AuthenticodeSignature -LiteralPath $env:BIRUNI_SIGNED; "
          "if($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'Cloudflare'){'ok'}else{$s.Status}")
    # by its full path: the engine's environment replaces PATH, and a bare "powershell" is then not found
    # (the check failed and every download was refused)
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                       "powershell.exe")
    if not os.path.isfile(exe):
        exe = "powershell"
    try:
        out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW,
                             env=dict(os.environ, BIRUNI_SIGNED=path))
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip() == "ok"


def download(folder=None, progress=None, cancelled=lambda: False):
    """Download and check cloudflared; returns its path. Raises RuntimeError with a reason."""
    folder = folder or tools_folder()
    os.makedirs(folder, exist_ok=True)
    final = os.path.join(folder, EXE)
    part = final + ".part"
    req = urllib.request.Request(DOWNLOAD, headers={"User-Agent": "Biruni-GIS"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancelled():
                    raise RuntimeError("cancelled")
                b = r.read(1 << 16)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if progress and total:
                    progress(done, total)
    except OSError as e:
        _remove(part)
        raise RuntimeError(str(e)) from e
    except RuntimeError:
        _remove(part)
        raise
    if not signed_by_cloudflare(part):
        _remove(part)
        raise RuntimeError("the downloaded file is not signed by Cloudflare")
    os.replace(part, final)
    return final


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class _Job:
    """Windows job object: its processes end when the last handle (this process) closes."""

    def __init__(self):
        self.handle = None
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = Extended()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        h = k.CreateJobObjectW(None, None)
        if h and k.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):  # extended limits
            self.handle, self._k = h, k

    def add(self, proc):
        if self.handle:
            self._k.AssignProcessToJobObject(self.handle, int(proc._handle))


_JOB = None


class Tunnel:
    """cloudflared tunnel to a local HTTPS port: a quick tunnel, or the user's own one when
    token and host are given. Events in self.events: ("url", address) once it can be reached,
    ("error", text), ("ended", code)."""

    def __init__(self, exe, port, token=None, host=None, scheme="https"):
        global _JOB
        self.events = queue.Queue()
        self.url = None
        self.fixed = f"https://{host}" if token else None
        env = dict(os.environ)
        if token:  # in the environment, not on the command line other programs can read
            cmd = [exe, "tunnel", "--no-autoupdate", "run"]
            env["TUNNEL_TOKEN"] = token
        else:
            cmd = [exe, "tunnel", "--no-autoupdate", "--url", f"{scheme}://127.0.0.1:{port}", "--no-tls-verify"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=NO_WINDOW, env=env)
        try:
            if _JOB is None:
                _JOB = _Job()
            _JOB.add(self.proc)
        except Exception:  # noqa: BLE001 - only a safety net; stop() still ends it
            pass
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        found = self.fixed
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace")
            m = URL_RE.search(line)
            if m and found is None:
                found = m.group(0)
            if found and self.url is None and READY_RE.search(line):
                self.url = found
                self.events.put(("url", found))
            elif FAIL_RE.search(line):
                self.events.put(("error", line.strip()[-200:]))
        self.events.put(("ended", self.proc.wait()))

    def running(self):
        return self.proc.poll() is None

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            self.proc.stderr.close()   # the reading thread ends with the process
        except (OSError, ValueError):
            pass
