"""restic as a child process. The password goes in the child's RESTIC_PASSWORD only: never in argv, never in a log.

``Restic(exe_or_argv, repo, password)``: a str is the program's path (it is re-hashed against its pin before every
run, tools.verified); a list is a ready command prefix (tests run a fake as [sys.executable, fake_restic.py]).
Exit codes: 0 ok, 3 some files unreadable (a snapshot was still made), 10 no repository, 11 locked, 12 wrong password.
"""
import contextlib
import json
import os
import subprocess

OK, PARTIAL, NO_REPO, LOCKED, WRONG_PASSWORD = 0, 3, 10, 11, 12
_CLEAN_ENV = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "TEMP", "TMP", "USERPROFILE",
              "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "USERNAME", "COMPUTERNAME")


class ResticError(Exception):
    def __init__(self, code, message=""):
        super().__init__(f"restic exit {code}: {message}".strip())
        self.code = code
        self.message = message


def clean_env(extra=None):
    """A child's environment: a short list of what Windows programs need, no proxy or credentials of ours."""
    env = {k: os.environ[k] for k in _CLEAN_ENV if k in os.environ}
    env.update(extra or {})
    return env


def _flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class Restic:
    def __init__(self, exe_or_argv, repo, password, env=None, tool="restic", pins=None):
        self.argv0 = list(exe_or_argv) if isinstance(exe_or_argv, (list, tuple)) else None
        self.exe = None if self.argv0 is not None else str(exe_or_argv)
        self.tool, self.pins = tool, pins
        self.repo = str(repo)
        self._password = str(password)
        self.extra_env = dict(env or {})

    # ---- the process ----------------------------------------------------------------------------------------------
    def _argv(self, args):
        if self.argv0 is not None:
            head = list(self.argv0)
        else:
            from . import tools

            head = [tools.verified(self.exe, self.tool, self.pins)]   # re-hashed before every spawn
        return head + ["-r", self.repo, "--no-cache"] + [str(a) for a in args]

    def _popen(self, args, stdout=subprocess.PIPE):
        env = clean_env(self.extra_env)
        env["RESTIC_PASSWORD"] = self._password
        return subprocess.Popen(self._argv(args), stdout=stdout, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                env=env, creationflags=_flags())

    def run(self, args, ok=(OK,)):
        """(code, stdout text, stderr text); ResticError when the code is not one of ``ok``."""
        p = self._popen(args)
        out, err = p.communicate()
        code, out, err = p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        if code not in ok:
            raise ResticError(code, self._scrub(err.strip().splitlines()[-1] if err.strip() else ""))
        return code, out, err

    def _scrub(self, text):
        return text.replace(self._password, "***") if self._password else text

    # ---- commands -------------------------------------------------------------------------------------------------
    def exists(self):
        """True when there is a repository here and the password opens it; False for no repository.
        ResticError for a wrong password (12) or a lock (11)."""
        code, _o, _e = self.run(["cat", "config"], ok=(OK, NO_REPO))
        return code == OK

    def init(self):
        self.run(["init"])

    def backup(self, paths, tag="biruni", on_event=None):
        """-> {"code", "snapshot", "unreadable": [paths], "new": files, "bytes": n}. Code 3 is returned, not raised."""
        p = self._popen(["backup", "--json", "--tag", tag] + [str(x) for x in paths])
        res = {"code": 0, "snapshot": "", "unreadable": [], "new": 0, "bytes": 0}
        for raw in p.stdout:
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("message_type")
            if kind == "error":
                item = str(ev.get("item") or "") or str((ev.get("error") or {}).get("message") or "")
                if item:
                    res["unreadable"].append(item)
            elif kind == "summary":
                res["snapshot"] = str(ev.get("snapshot_id") or "")
                res["new"] = int(ev.get("files_new") or 0)
                res["bytes"] = int(ev.get("total_bytes_processed") or 0)
            if on_event is not None:
                on_event(ev)
        err = p.stderr.read().decode("utf-8", "replace")
        p.wait()
        res["code"] = p.returncode
        if p.returncode not in (OK, PARTIAL):
            raise ResticError(p.returncode, self._scrub(err.strip().splitlines()[-1] if err.strip() else ""))
        return res

    def snapshots(self):
        _c, out, _e = self.run(["snapshots", "--json"])
        try:
            rows = json.loads(out or "[]")
        except ValueError:
            rows = []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def ls(self, snap):
        _c, out, _e = self.run(["ls", "--json", snap])
        rows = []
        for line in out.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("path"):
                rows.append(r)
        return rows

    def check(self, subset=None):
        """(ok, last line of the report); a failed check is a result, not an exception."""
        args = ["check"] + ([f"--read-data-subset={subset}"] if subset else [])
        p = self._popen(args)
        out, err = p.communicate()
        text = (out + err).decode("utf-8", "replace").strip()
        if p.returncode in (WRONG_PASSWORD, NO_REPO, LOCKED):
            raise ResticError(p.returncode, self._scrub(text.splitlines()[-1] if text else ""))
        return p.returncode == OK, self._scrub(text.splitlines()[-1] if text else "")

    def restore(self, snap, include, target):
        self.run(["restore", snap, "--target", target, "--include", include, "--verify"])

    @contextlib.contextmanager
    def dump_tar(self, snap, folder):
        """The tar of one folder of a snapshot, streamed: ``with r.dump_tar(snap, "/C/photos/2026-11-24") as f:``,
        then tarfile.open(fileobj=f, mode="r|"). The exit code is checked when the block ends."""
        p = self._popen(["dump", "--archive", "tar", snap, folder])
        try:
            yield p.stdout
            while p.stdout.read(1 << 20):    # the reader may stop at the tar's end marker: drain the rest
                pass
        finally:
            with contextlib.suppress(OSError):
                p.stdout.close()
            err = p.stderr.read().decode("utf-8", "replace") if p.stderr else ""
            p.wait()
        if p.returncode != OK:
            raise ResticError(p.returncode, self._scrub(err.strip().splitlines()[-1] if err.strip() else ""))

    def forget_prune(self, policy):
        """Only engine.admin_prune calls this (after the administrator typed the organisation's name)."""
        args = ["forget", "--prune"]
        for k, v in (policy or {}).items():
            args += [f"--{k}", str(v)]
        self.run(args)

    def unlock(self):
        self.run(["unlock"])
