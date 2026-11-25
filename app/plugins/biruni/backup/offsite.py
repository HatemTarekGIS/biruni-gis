r"""Off-site copy of the restic repository via rclone (no-loss P9 stage 9d).

Rules:
- Runs ``rclone copy --immutable <restic repo> <remote>`` — NEVER ``sync``, never ``delete`` or ``purge``.
- The rclone configuration is encrypted with ``RCLONE_CONFIG_PASS``, kept under DPAPI in ``<cert>\rclone_pw.dat`` and
  carried inside key backup v2 (``body["rclone_pw"]``).
- ``RCLONE_CONFIG_PASS`` goes in the child process's environment only: never in argv, never in ``state.json`` or ``log.jsonl``.
- The remote itself is configured by the office through rclone's own interactive ``config`` flow launched in a console
  window; Biruni stores only the remote target (e.g. ``office-cloud:biruni-backup``).
"""
import os
import secrets
import subprocess
import time

from .restic import _flags, clean_env

PW_FILE = "rclone_pw.dat"
CONF_FILE = "rclone.conf"


class OffsiteError(Exception):
    def __init__(self, code, message=""):
        super().__init__(f"rclone exit {code}: {message}".strip())
        self.code = code
        self.message = message


def pw_path(cert_folder):
    return os.path.join(cert_folder, PW_FILE)


def config_path(cert_folder):
    return os.path.join(cert_folder, "backup", CONF_FILE)


def load_password(cert_folder):
    """The rclone config password, or ``""`` when none exists or it cannot be unprotected."""
    from ..field_photos.tunnel import unprotect

    try:
        with open(pw_path(cert_folder), encoding="ascii") as f:
            return unprotect(f.read().strip())
    except OSError:
        return ""


def _write_password(cert_folder, pw):
    from ..field_photos.tunnel import protect

    os.makedirs(cert_folder, exist_ok=True)
    p = pw_path(cert_folder)
    with open(p + ".tmp", "w", encoding="ascii") as f:
        f.write(protect(pw))
    os.replace(p + ".tmp", p)


def ensure_password(cert_folder):
    """``(password, created)``. An unreadable existing file is moved aside first, never overwritten."""
    pw = load_password(cert_folder)
    if pw:
        return pw, False
    p = pw_path(cert_folder)
    if os.path.isfile(p):
        os.replace(p, p + ".unreadable-" + time.strftime("%Y%m%d-%H%M%S"))
    pw = secrets.token_urlsafe(24)
    _write_password(cert_folder, pw)
    return pw, True


def install_password(cert_folder, pw):
    """Install ``pw`` from a key backup; keep any differing existing password as ``.old``."""
    cur = load_password(cert_folder)
    if cur == pw:
        return False
    p = pw_path(cert_folder)
    if cur and os.path.isfile(p):
        os.replace(p, p + "." + time.strftime("%Y%m%d-%H%M%S") + ".old")
    _write_password(cert_folder, pw)
    return True


def normalize_remote(remote):
    remote = str(remote or "").strip()
    if not remote:
        return ""
    return remote if ":" in remote else remote + ":biruni-backup"


class Rclone:
    """rclone child-process wrapper.

    ``exe_or_argv``: path to ``rclone.exe`` (re-verified against ``tools.PINNED`` before every spawn), or a list
    command prefix for tests (e.g. ``[sys.executable, fake_rclone.py]``).
    """

    def __init__(self, exe_or_argv=None, cert_folder="", password="", config_file="", env=None,
                 tool="rclone", pins=None):
        self.argv0 = list(exe_or_argv) if isinstance(exe_or_argv, (list, tuple)) else None
        self.exe = None if self.argv0 is not None else (str(exe_or_argv) if exe_or_argv else None)
        self.tool, self.pins = tool, pins
        self.cert = cert_folder
        self._password = str(password or "")
        self.config_file = config_file or (config_path(cert_folder) if cert_folder else "")
        self.extra_env = dict(env or {})
        self.last_argv = []

    def _password_value(self):
        if self._password:
            return self._password
        if self.cert:
            pw, _ = ensure_password(self.cert)
            self._password = pw
            return pw
        return ""

    def _head(self):
        if self.argv0 is not None:
            return list(self.argv0)
        from . import tools

        exe = self.exe or tools.find(self.tool, self.pins)
        if not exe:
            raise tools.ToolError("rclone is not installed on this computer yet")
        return [tools.verified(exe, self.tool, self.pins)]

    def _argv(self, args):
        if any(str(a) in ("sync", "delete", "deletefile", "purge") for a in args):
            raise ValueError("rclone sync/delete is forbidden; Biruni only runs copy --immutable")
        head = self._head()
        cfg = ["--config", self.config_file] if self.config_file else []
        argv = head + cfg + [str(a) for a in args]
        self.last_argv = list(argv)
        return argv

    def _env(self):
        env = clean_env(self.extra_env)
        pw = self._password_value()
        if pw:
            env["RCLONE_CONFIG_PASS"] = pw
        return env

    def _scrub(self, text):
        pw = self._password
        return text.replace(pw, "***") if pw and text else text

    def run(self, args):
        if self.config_file:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        p = subprocess.Popen(
            self._argv(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=self._env(),
            creationflags=_flags(),
        )
        out, err = p.communicate()
        code = p.returncode
        out_s = self._scrub(out.decode("utf-8", "replace"))
        err_s = self._scrub(err.decode("utf-8", "replace"))
        if code != 0:
            last = (err_s.strip() or out_s.strip()).splitlines()[-1] if (err_s.strip() or out_s.strip()) else ""
            raise OffsiteError(code, last)
        return code, out_s, err_s

    def copy_immutable(self, repo, remote):
        """Copy the restic repository ``repo`` to ``remote`` using ``rclone copy --immutable``."""
        repo = str(repo or "").strip()
        target = normalize_remote(remote)
        if not repo or not target:
            raise ValueError("both repository and remote are required")
        return self.run(["copy", "--immutable", repo, target])

    def open_config_console(self):
        """Launch ``rclone config`` in its own console window so the office can configure its cloud remote."""
        if self.config_file:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        flag = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
        return subprocess.Popen(self._argv(["config"]), env=self._env(), creationflags=flag)


def run_offsite(cert_folder, repo, remote, rclone=None, now=time.time):
    """Run ``rclone copy --immutable <repo> <remote>`` and record ``last_offsite`` in ``state.json`` and ``log.jsonl``."""
    from . import engine

    now_s = float(now() if callable(now) else now)
    target = normalize_remote(remote)
    if not repo or not target:
        return {"status": "not-configured"}
    if not engine.target_present(repo):
        return {"status": "deferred", "reason": "target absent"}
    rc = rclone if rclone is not None else Rclone(cert_folder=cert_folder)
    try:
        rc.copy_immutable(repo, target)
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", 1)
        msg = rc._scrub(str(e))[:300]
        st = engine.load_state(cert_folder)
        prev = st.get("last_offsite") or {}
        st["last_offsite"] = {
            "at": now_s,
            "ok": False,
            "last_ok_at": prev.get("last_ok_at") or (prev.get("at") if prev.get("ok") else None),
            "code": code,
            "error": msg,
            "remote": target,
        }
        engine.save_state(cert_folder, st)
        engine.append_log(cert_folder, "offsite", ok=False, code=code, error=msg, remote=target)
        return {"status": "error", "code": code, "error": msg}

    st = engine.load_state(cert_folder)
    st["last_offsite"] = {"at": now_s, "ok": True, "last_ok_at": now_s, "code": 0, "error": "", "remote": target}
    engine.save_state(cert_folder, st)
    engine.append_log(cert_folder, "offsite", ok=True, code=0, remote=target)
    return {"status": "ok", "remote": target}
