"""WhiteboxTools, the free and open core (MIT), run as its command-line program.

Only the open core ships with Biruni (scripts/engine-tools.txt): never Whitebox Workflows, the Whitebox Toolset
Extension or anything that needs a licence key. The core tools used here are all part of it.
"""
import os

from . import external_tools as xt

TOOL = "whitebox"


class WhiteboxError(Exception):
    pass


def available():
    return xt.exe(TOOL) is not None


def version():
    exe = xt.exe(TOOL)
    if exe is None:
        return ""
    code, out = xt.run([exe, "--version"], cwd=os.path.dirname(exe), timeout=30)
    return out.splitlines()[0].strip() if code == 0 and out else ""


def run(tool, wd, feedback=None, span=(0, 100), **args):
    """Run one WhiteboxTools tool in the folder ``wd`` (file names relative to it). ``args``: --name=value
    (True: just --name; None/False: left out). Its progress moves ``feedback`` within ``span``."""
    exe = xt.exe(TOOL)
    if exe is None:
        raise WhiteboxError("missing")
    cmd = [exe, f"--run={tool}", f"--wd={wd}", "-v"]
    for k, v in args.items():
        if v is None or v is False:
            continue
        cmd.append(f"--{k}" if v is True else f"--{k}={v}")
    a, b = span

    def progress(p):
        if feedback is not None:
            feedback.setProgress(a + (b - a) * p / 100.0)

    code, out = xt.run(cmd, cwd=wd, feedback=feedback, progress=progress)
    if code is None:
        raise WhiteboxError("cancelled")
    bad = [l for l in out.splitlines() if l.lower().startswith("error") or "panicked" in l]
    if code != 0 or bad:
        raise WhiteboxError(f"{tool}: " + ("\n".join(bad) or out.strip()[-500:]))
    return out
