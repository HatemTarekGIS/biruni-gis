"""Which copy of Biruni runs: the one installed with the program, or a newer one an update put in the user's folder.

Read once, before anything else of Biruni is imported (``biruni/__init__.py``), so it uses nothing but the
standard library. The state of the updates is one small file, ``state.json``, in the updates folder:

    {"current": "0.2.3", "previous": "0.2.2", "next": "0.2.4", "trial": false, "bad": ["0.2.1"]}

* ``next`` - staged by an update and verified: it becomes current at this start, on trial.
* ``trial`` - a version on trial that did not say it started well (``healthy``) before Biruni was opened again
  is taken for broken: it goes to ``bad`` and the previous copy runs instead.
* A copy is used only when it is newer than the installed one: installing a newer Biruni wins over any update.
"""
import json
import os
import re
import shutil
import time

STATE = "state.json"
MARK = ".biruni-update.json"     # in each staged copy: its version and that it was verified


def root():
    """The updates folder, or None when this is not an installed Biruni (tests and development use the code as
    it is). BIRUNI_UPDATES may point it elsewhere (tests)."""
    if os.environ.get("BIRUNI_UPDATES"):
        return os.environ["BIRUNI_UPDATES"]
    if not os.environ.get("BIRUNI_HOME") or os.environ.get("BIRUNI_NO_UPDATES"):
        return None
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "Biruni GIS", "updates")


def parse_version(text):
    nums = [int(n) for n in re.findall(r"\d+", str(text or ""))[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def folder_version(plugin_dir):
    """The VERSION in a copy's version.py (read as text, nothing imported)."""
    try:
        with open(os.path.join(plugin_dir, "version.py"), encoding="utf-8") as f:
            m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def copy_dir(updates, version):
    return os.path.join(updates, version, "biruni")


def read_state(updates):
    try:
        with open(os.path.join(updates, STATE), encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(updates, state):
    os.makedirs(updates, exist_ok=True)
    tmp = os.path.join(updates, STATE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, os.path.join(updates, STATE))


def usable(updates, version):
    d = copy_dir(updates, version or "")
    if not version or not os.path.isfile(os.path.join(d, "__init__.py")):
        return False
    try:
        with open(os.path.join(d, MARK), encoding="utf-8") as f:
            mark = json.load(f)
    except (OSError, ValueError):
        return False
    return mark.get("version") == version and mark.get("verified") is True and folder_version(d) == version


def pick(installed_dir):
    """The folder of the copy to run (a staged update), or None for the installed one."""
    updates = root()
    if not updates or not os.path.isdir(updates):
        return None
    state = read_state(updates)
    bad = list(state.get("bad") or [])
    changed = False
    if state.get("trial") and state.get("current"):
        # the version on trial never said it started well: back to the one before
        bad.append(state["current"])
        state.update(current=state.get("previous"), previous=None, trial=False, bad=bad[-20:])
        changed = True
    nxt = state.get("next")
    if nxt:
        if nxt not in bad and usable(updates, nxt):
            if state.get("current") != nxt:
                state["previous"] = state.get("current")
            state.update(current=nxt, trial=True, trial_since=time.time())
        state["next"] = None
        changed = True
    cur = state.get("current")
    installed = parse_version(folder_version(installed_dir))
    chosen = None
    if cur and cur not in bad and usable(updates, cur) and parse_version(cur) > installed:
        chosen = copy_dir(updates, cur)
    elif state.get("trial"):
        state["trial"] = False   # nothing to try: the installed copy runs
        changed = True
    if changed:
        try:
            write_state(updates, state)
        except OSError:
            pass
    return chosen


def healthy():
    """Biruni started well with the copy it runs: the trial is over, and copies other than this one and the one
    before are removed."""
    updates = root()
    if not updates or not os.path.isdir(updates):
        return
    state = read_state(updates)
    if state.get("trial"):
        state["trial"] = False
        write_state(updates, state)
    keep = {state.get("current"), state.get("previous"), state.get("next")}
    for name in os.listdir(updates):
        p = os.path.join(updates, name)
        if os.path.isdir(p) and name not in keep and not name.startswith("_"):
            shutil.rmtree(p, ignore_errors=True)
