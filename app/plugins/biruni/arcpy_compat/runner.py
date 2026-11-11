"""Run an arcpy script in this process with the shim installed as `arcpy`."""
import builtins
import contextlib
import io
import os
import sys
import traceback

from . import _base
from .py2fix import PY2_BUILTINS, fix_python2


def read_source(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def compile_script(text, path):
    """-> (code object, python2 flag). Python 2 scripts get the light print/except fix-up."""
    try:
        return compile(text, path, "exec"), False
    except SyntaxError as first:
        fixed, changed = fix_python2(text)
        if not changed:
            raise
        try:
            return compile(fixed, path, "exec"), True
        except SyntaxError:
            raise first from None


class _LineWriter(io.TextIOBase):
    """print() inside the script -> message callback, line by line."""

    def __init__(self, severity, emit):
        self._sev = severity
        self._emit = emit
        self._buf = ""

    def writable(self):
        return True

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(self._sev, line.rstrip("\r"))
        return len(s)

    def flush(self):
        if self._buf:
            self._emit(self._sev, self._buf)
            self._buf = ""


class ScriptCancelled(BaseException):
    """BaseException so neither the script's `except Exception` nor the message plumbing swallows it."""


def run_script(path, args=None, on_message=None, on_progress=None, workspace=None, cancel_check=None):
    """Execute an arcpy script. Returns {"ok", "error", "outputs", "created", "messages", "python2"}."""
    from . import data as D
    from . import install, is_installed, uninstall

    path = os.path.abspath(path)
    args = [str(a) for a in (args or [])]
    messages = []

    def emit(sev, text):
        messages.append((sev, text))
        if on_message is not None:
            on_message(sev, text)
        if cancel_check is not None and cancel_check():
            raise ScriptCancelled("Cancelled")

    def progress(kind, value):
        if on_progress is not None:
            on_progress(kind, value)
        if cancel_check is not None and cancel_check():
            raise ScriptCancelled("Cancelled")

    result = {"ok": False, "error": None, "outputs": {}, "created": [], "messages": messages, "python2": False}
    text = read_source(path)
    try:
        code, py2 = compile_script(text, path)
    except SyntaxError as e:
        result["error"] = f"Syntax error in {os.path.basename(path)} line {e.lineno}: {e.msg}"
        emit(2, result["error"])
        return result
    result["python2"] = py2
    if py2:
        emit(1, "This is a Python 2 script; print statements and other old syntax were converted on the fly.")

    was_installed = is_installed()
    arcpy = install()
    _base.env.reset()
    if workspace:
        _base.env.workspace = workspace
    D.LAYERS.clear()
    del _base.CREATED[:]
    _base.set_script_parameters(args)
    _base.add_handler(emit)
    _base.add_progress_handler(progress)
    old_argv, old_path, old_cwd = list(sys.argv), list(sys.path), os.getcwd()
    sys.argv = [path] + args
    sys.path.insert(0, os.path.dirname(path))
    ns = {"__name__": "__main__", "__file__": path, "__builtins__": builtins, "arcpy": arcpy}
    if py2:
        ns.update(PY2_BUILTINS)
    out_w, err_w = _LineWriter(0, emit), _LineWriter(1, emit)
    try:
        with contextlib.redirect_stdout(out_w), contextlib.redirect_stderr(err_w):
            try:
                exec(code, ns)  # noqa: S102 - running the user's script is the point
                result["ok"] = True
            except SystemExit as e:
                result["ok"] = e.code in (None, 0)
                if not result["ok"]:
                    result["error"] = f"The script stopped with exit code {e.code}"
            except ScriptCancelled:
                result["error"] = "Cancelled"
            except _base.ExecuteError as e:
                result["error"] = str(e)
            except Exception as e:  # noqa: BLE001 - report any script error
                tb = traceback.extract_tb(e.__traceback__)
                where = next((f for f in reversed(tb) if os.path.abspath(f.filename) == path), None)
                loc = f" (line {where.lineno})" if where else ""
                result["error"] = f"{type(e).__name__}: {e}{loc}"
            finally:
                out_w.flush()
                err_w.flush()
    finally:
        for c in list(D._LIVE):
            try:
                c._close()
            except Exception:
                pass
        sys.argv = old_argv
        sys.path[:] = old_path
        try:
            os.chdir(old_cwd)
        except OSError:
            pass
        _base.remove_handler(emit)
        _base.remove_progress_handler(progress)
        result["outputs"] = _base.script_outputs()
        result["created"] = list(_base.CREATED)
        _base.set_script_parameters(None)
        if not was_installed:
            uninstall()
    if result["error"] and result["error"] != "Cancelled":
        if on_message is not None:
            on_message(2, result["error"])
        messages.append((2, result["error"]))
    return result
