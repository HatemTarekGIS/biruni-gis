r"""A stand-in for restic in tests (no network, no binary): run as [sys.executable, fake_restic.py].

The repository is a folder: config.json {"pw": sha256(password)}, snaps/<id>/ (a copy of each source, under the path restic
would show: C:\a\b -> C/a/b), snaps/<id>.json (tag, time, paths), calls.log (one line per call: the arguments only, never
the password). Switches are files in the repository: FAIL_CHECK (check fails), LOCK (exit 11).
Files whose name ends with .locked are "unreadable": backup still makes a snapshot and exits 3.
"""
import hashlib
import json
import os
import shutil
import sys
import tarfile
import time


def snap_rel(p):
    p = os.path.abspath(p)
    drive, rest = os.path.splitdrive(p)
    return (drive.rstrip(":") + rest).replace("\\", "/").strip("/")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main(argv):
    args = list(argv[1:])
    repo = ""
    if "-r" in args:
        i = args.index("-r")
        repo = args[i + 1]
        del args[i:i + 2]
    args = [a for a in args if a != "--no-cache"]
    cmd, rest = args[0], args[1:]
    pw = os.environ.get("RESTIC_PASSWORD", "")
    if os.path.isdir(repo):
        with open(os.path.join(repo, "calls.log"), "a", encoding="utf-8") as f:
            f.write(" ".join([cmd] + rest) + "\n")
    cfg_path = os.path.join(repo, "config.json")
    if cmd == "init":
        if os.path.exists(cfg_path):
            sys.stderr.write("config already initialized\n")
            return 1
        os.makedirs(os.path.join(repo, "snaps"), exist_ok=True)
        json.dump({"pw": hashlib.sha256(pw.encode()).hexdigest()}, open(cfg_path, "w"))
        return 0
    if not os.path.exists(cfg_path):
        sys.stderr.write("Fatal: unable to open config file: repository does not exist\n")
        return 10
    if json.load(open(cfg_path))["pw"] != hashlib.sha256(pw.encode()).hexdigest():
        sys.stderr.write("Fatal: wrong password or no key found\n")
        return 12
    if os.path.exists(os.path.join(repo, "LOCK")):
        sys.stderr.write("Fatal: repository is already locked\n")
        return 11
    snaps = os.path.join(repo, "snaps")
    if cmd == "cat":
        out({"version": 2})
        return 0
    if cmd == "backup":
        tag = rest[rest.index("--tag") + 1]
        paths = [a for a in rest if a not in ("--json", "--tag", tag)]
        sid = hashlib.sha256(f"{time.time()}{len(os.listdir(snaps))}".encode()).hexdigest()[:8]
        base = os.path.join(snaps, sid)
        bad, new = [], 0
        for src in paths:
            for root, _dirs, files in os.walk(src):
                for fn in files:
                    full = os.path.join(root, fn)
                    if fn.endswith(".locked"):
                        out({"message_type": "error", "item": full, "during": "archival", "error": {"message": "locked"}})
                        bad.append(full)
                        continue
                    dst = os.path.join(base, *snap_rel(full).split("/"))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copyfile(full, dst)
                    new += 1
        os.makedirs(base, exist_ok=True)
        json.dump({"id": sid, "tags": [tag], "time": time.strftime("%Y-%m-%dT%H:%M:%S") + ".%06d" % (len(os.listdir(snaps)) % 1000000),
                   "paths": paths}, open(base + ".json", "w"))
        out({"message_type": "summary", "snapshot_id": sid, "files_new": new, "total_bytes_processed": 0})
        return 3 if bad else 0
    if cmd == "snapshots":
        rows = [json.load(open(os.path.join(snaps, f))) for f in sorted(os.listdir(snaps)) if f.endswith(".json")]
        rows.sort(key=lambda r: r["time"])
        out(rows)
        return 0
    if cmd == "check":
        if os.path.exists(os.path.join(repo, "FAIL_CHECK")):
            sys.stdout.write("Fatal: pack 1234 failed\n")
            return 1
        sys.stdout.write("no errors were found\n")
        return 0
    if cmd == "dump":
        snap, folder = rest[-2], rest[-1]
        top = os.path.join(snaps, snap, *folder.strip("/").split("/"))
        tf = tarfile.open(fileobj=sys.stdout.buffer, mode="w|")
        for root, _dirs, files in os.walk(top):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                arc = os.path.basename(top) + "/" + os.path.relpath(full, top).replace("\\", "/")
                tf.add(full, arcname=arc)
        tf.close()
        sys.stdout.buffer.flush()
        return 0
    if cmd == "restore":
        snap = rest[0]
        target = rest[rest.index("--target") + 1]
        inc = rest[rest.index("--include") + 1]
        src = os.path.join(snaps, snap, *inc.strip("/").split("/"))
        dst = os.path.join(target, *inc.strip("/").split("/"))
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
        else:
            sys.stderr.write("nothing to restore\n")
            return 1
        return 0
    if cmd in ("forget", "unlock"):
        return 0
    if cmd == "ls":
        return 0
    sys.stderr.write("unknown command " + cmd + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
