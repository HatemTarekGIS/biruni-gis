"""Put the command-line tools of engine-tools.txt into <engine>/tools/<name>/ (safe to repeat).

    python -I add_tools.py <engine folder> <folder of the archives>

Every archive is checked against its sha256 first; a missing archive stops the build (a release without them
would lack its DWG and terrain tools). Licence texts beside this script (tool-licenses/<name>/) go with each tool.
"""
import fnmatch
import hashlib
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))


def entries(path=os.path.join(HERE, "engine-tools.txt")):
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, archive, sha, files = (p.strip() for p in line.split("|"))
        yield name, archive, sha.lower(), [f.strip() for f in files.split(";") if f.strip()]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install(engine, archives):
    for name, archive, sha, files in entries():
        src = os.path.join(archives, archive)
        if not os.path.isfile(src):
            raise SystemExit(f"missing archive: {src}")
        got = sha256(src)
        if got != sha:
            raise SystemExit(f"{archive}: sha256 {got}, expected {sha}")
        dest = os.path.join(engine, "tools", name)
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.makedirs(dest)
        taken = 0
        with zipfile.ZipFile(src) as z:
            for info in z.infolist():
                n = info.filename
                if info.is_dir() or n.startswith("__MACOSX/"):
                    continue
                if not any(fnmatch.fnmatch(n, pat) for pat in files):
                    continue
                out = os.path.join(dest, os.path.basename(n))
                with z.open(info) as a, open(out, "wb") as b:
                    shutil.copyfileobj(a, b)
                taken += 1
        if taken < len([f for f in files if not any(c in f for c in "*?[")]):
            raise SystemExit(f"{archive}: only {taken} of the files listed were found")
        lic = os.path.join(HERE, "tool-licenses", name)
        if os.path.isdir(lic):
            for f in os.listdir(lic):
                shutil.copy2(os.path.join(lic, f), os.path.join(dest, f))
        print(f"{name}: {taken} files -> {dest}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    install(sys.argv[1], sys.argv[2])
