"""Where the command-line tools shipped beside the engine live, and how they are run.

The release puts each in engine/tools/<name>/ (scripts/add_tools.py, from scripts/engine-tools.txt): the
free and open WhiteboxTools core and LibreDWG. BIRUNI_TOOLS (a folder holding <name>/ folders) overrides it,
for a developer's copy.
"""
import os
import re
import shutil
import subprocess

TOOLS = {"whitebox": "whitebox_tools.exe", "libredwg": "dwg2dxf.exe"}


def engine_root():
    from qgis.core import QgsApplication

    return os.path.normpath(os.path.join(QgsApplication.prefixPath(), "..", ".."))


def tool_dir(name):
    """The folder of a shipped tool, or None when it is not there."""
    exe = TOOLS.get(name, name)
    for base in filter(None, [os.environ.get("BIRUNI_TOOLS"), os.path.join(engine_root(), "tools")]):
        d = os.path.join(base, name)
        if os.path.isfile(os.path.join(d, exe)):
            return d
    return None


def exe(name, program=None):
    """The full path of a program of a shipped tool (its main one by default), or None."""
    d = tool_dir(name)
    if d is None:
        return None
    p = os.path.join(d, program or TOOLS.get(name, name))
    if os.path.isfile(p):
        return p
    found = shutil.which(program or TOOLS.get(name, name))
    return found


class ToolError(Exception):
    pass


_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


def run(args, cwd=None, feedback=None, progress=None, timeout=None, env=None):
    """Run a command line; returns (code, output). Lines with a percentage move ``progress(p)``; cancelling the
    feedback (or passing ``timeout`` seconds) stops the process (code None)."""
    import time

    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            creationflags=flags, bufsize=1, text=True, encoding="utf-8", errors="replace")
    out = []
    start = time.time()
    import threading

    def reader():
        for line in proc.stdout:
            out.append(line)
            if progress is not None:
                m = _PERCENT.search(line)
                if m:
                    try:
                        progress(min(100.0, float(m.group(1))))
                    except Exception:  # noqa: BLE001 - a progress callback never stops the tool
                        pass

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    while proc.poll() is None:
        th.join(0.2)
        if (feedback is not None and feedback.isCanceled()) or (timeout and time.time() - start > timeout):
            proc.kill()
            proc.wait()
            th.join(1)
            proc.stdout.close()
            return None, "".join(out)
    th.join(2)
    proc.stdout.close()
    return proc.returncode, "".join(out)
