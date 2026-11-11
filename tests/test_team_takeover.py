r"""A team room on three computers (three processes on this PC): the host's computer is switched off in the middle
of the work; the first member in the join order becomes the host by itself, the other member follows it, and the
room goes on; the old host comes back, sees that the room moved, joins the new host as a member and gets what
it missed.

Run:  engine\bin\python-qgis.bat tests\test_team_takeover.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PEER = os.path.join(HERE, "team_peer.py")
PY = sys.executable
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


root = tempfile.mkdtemp()
dirs = {n: os.path.join(root, n) for n in ("Hana", "Badr", "Celine")}
for d in dirs.values():
    os.makedirs(d)
procs = {}
logs = {}


def start(name, *args):
    logs[name] = open(os.path.join(root, name + ".log"), "a")
    procs[name] = subprocess.Popen([PY, PEER, dirs[name], *args], stdout=logs[name], stderr=subprocess.STDOUT,
                                   env=dict(os.environ, PYTHONIOENCODING="utf-8"))


def status(name):
    try:
        with open(os.path.join(dirs[name], "status.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def wait(cond, seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return False


cmds = {n: [] for n in dirs}


def command(name, **c):
    c["id"] = len(cmds[name]) + 1
    cmds[name].append(c)
    with open(os.path.join(dirs[name], "cmd.json"), "w") as f:
        json.dump(cmds[name], f)


try:
    start("Hana", "host")
    ok = wait(lambda: os.path.isfile(os.path.join(dirs["Hana"], "code.txt")), 60)
    check(ok, "Hana opened a room")
    code = open(os.path.join(dirs["Hana"], "code.txt")).read().strip()
    start("Badr", "join", code)
    check(wait(lambda: status("Badr").get("state") == "connected", 90), "Badr joined")
    start("Celine", "join", code)
    check(wait(lambda: status("Celine").get("state") == "connected", 90), "Celine joined (after Badr)")

    command("Celine", op="edit", fid=1, owner="before the power cut")
    check(wait(lambda: status("Hana")["owners"]["1"] == "before the power cut"
               and status("Badr")["owners"]["1"] == "before the power cut", 30), "an edit reaches everyone")

    # the last moment: the host and a member both edit, and the power goes 1.8 s later (the host's edit is saved by
    # itself after 1.5 s; the member's may or may not have been written by the host)
    command("Hana", op="edit", fid=4, owner="Hana just before the cut")
    command("Celine", op="edit", fid=5, owner="Celine just before the cut")
    time.sleep(1.8)
    procs["Hana"].kill()  # the host's computer is switched off (power cut)
    procs["Hana"].wait()
    t0 = time.time()
    ok = wait(lambda: status("Badr").get("role") == "host", 90)
    check(ok, f"Badr, first in the join order, became the host by itself ({time.time() - t0:.0f} s)")
    check(wait(lambda: status("Celine").get("state") == "connected" and status("Celine").get("epoch") == 2, 90),
          "Celine found the new host and follows it")
    command("Celine", op="edit", fid=2, owner="after the power cut")
    check(wait(lambda: status("Badr")["owners"]["2"] == "after the power cut", 30), "the room goes on: Celine's edit "
                                                                                     "reaches Badr")
    command("Badr", op="edit", fid=3, owner="by the new host")
    check(wait(lambda: status("Celine")["owners"]["3"] == "by the new host", 30), "and Badr's reaches Celine")

    start("Hana", "reopen")  # the old host comes back
    ok = wait(lambda: status("Hana").get("role") == "member" and status("Hana").get("state") == "connected"
              and status("Hana").get("epoch") == 2, 120)
    check(ok, "Hana, back, found that the room moved and joined Badr as a member")
    check(wait(lambda: status("Hana")["owners"]["2"] == "after the power cut"
               and status("Hana")["owners"]["3"] == "by the new host", 30), "Hana got what she missed")
    check(status("Badr").get("role") == "host", "Badr is still the host (one host only)")
    check(wait(lambda: all(status(n)["owners"]["5"] == "Celine just before the cut" for n in dirs), 30),
          "the member's edit made just before the power cut is everywhere (nothing lost)")
    check(wait(lambda: all(status(n)["owners"]["4"] == "Hana just before the cut" for n in dirs), 30),
          "the host's own edit made just before the power cut is everywhere once it is back")
    command("Hana", op="edit", fid=1, owner="Hana is back")
    check(wait(lambda: status("Celine")["owners"]["1"] == "Hana is back", 30), "Hana's edits reach the others again")

    # a member closes Biruni and opens the room's map again later
    command("Celine", op="quit")
    check(wait(lambda: procs["Celine"].poll() is not None, 30), "Celine closed Biruni")
    command("Badr", op="edit", fid=2, owner="while Celine was away")
    time.sleep(3)
    cmds["Celine"] = []
    os.remove(os.path.join(dirs["Celine"], "cmd.json"))
    os.remove(os.path.join(dirs["Celine"], "status.json"))
    start("Celine", "reopen")
    check(wait(lambda: status("Celine").get("state") == "connected" and status("Celine").get("role") == "member", 90),
          "Celine opened the room's map again and was connected by itself")
    check(wait(lambda: status("Celine")["owners"]["2"] == "while Celine was away", 30), "and got what changed while away")
    command("Celine", op="edit", fid=3, owner="Celine is back")
    check(wait(lambda: status("Badr")["owners"]["3"] == "Celine is back", 30), "her edits reach the host again")
finally:
    for name, p in procs.items():
        if p.poll() is None:
            command(name, op="quit")
    time.sleep(3)
    for p in procs.values():
        if p.poll() is None:
            p.kill()
    for f in logs.values():
        f.close()
    if failures:
        for n in dirs:
            print(n, status(n))
            print(f"---- {n} log ----")
            print(open(os.path.join(root, n + ".log"), encoding="utf-8", errors="replace").read()[-3000:])
    shutil.rmtree(root, ignore_errors=True)

print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'ALL PASSED'}")
sys.exit(1 if failures else 0)
