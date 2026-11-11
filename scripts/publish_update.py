r"""Publish an update of Biruni's own code: only what changed reaches the users (app/plugins/biruni/update/).

Writes into SITE/updates/ (the gh-pages checkout, served at version.UPDATE_URL):
  manifest.json       every file of app/plugins/biruni with its SHA-256 and size, the version, notes
  manifest.sig        its Ed25519 signature (hex) with the key in _secrets/update-signing.key
  files/ab/<sha256>   each file once, by its SHA-256 (a file already there is not written again)
  manifest-<version>.json, a copy kept

Bump VERSION in app/plugins/biruni/version.py first: a version not newer than the one published is refused.

usage:
  engine\bin\python-qgis.bat scripts\publish_update.py SITE --notes-ar "..." --notes-en "..."
  engine\bin\python-qgis.bat scripts\publish_update.py --new-key      (once: makes the key, prints the public half)
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PLUGIN = os.path.join(ROOT, "app", "plugins", "biruni")
KEY = os.path.join(ROOT, "_secrets", "update-signing.key")
sys.path.insert(0, os.path.join(ROOT, "app", "plugins"))
sys.path.insert(0, os.path.join(ROOT, "app", "python"))
os.environ.setdefault("BIRUNI_NO_UPDATES", "1")   # this script uses the code as it is in the repository

from biruni.secure import crypto  # noqa: E402
from biruni.update import boot, client  # noqa: E402


def new_key(path):
    if os.path.exists(path):
        sys.exit(f"{path} exists already: a new key would make every Biruni refuse the updates signed with it")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    seed = os.urandom(32)
    with open(path, "w", encoding="ascii") as f:
        f.write(seed.hex() + "\n")
    print("key written to", path)
    print("public key (put it in version.py as UPDATE_KEY):", crypto.public_key(seed).hex())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site", nargs="?")
    ap.add_argument("--notes-ar", default="")
    ap.add_argument("--notes-en", default="")
    ap.add_argument("--engine", default="", help="the oldest engine version this code runs on")
    ap.add_argument("--key", default=KEY)
    ap.add_argument("--new-key", action="store_true")
    ap.add_argument("--source", default=PLUGIN)
    a = ap.parse_args()
    if a.new_key:
        return new_key(a.key)
    if not a.site:
        ap.error("SITE is needed")
    with open(a.key, encoding="ascii") as f:
        seed = bytes.fromhex(f.read().strip())
    pub = crypto.public_key(seed).hex()
    import re

    with open(os.path.join(a.source, "version.py"), encoding="utf-8") as f:
        want = re.search(r'^UPDATE_KEY\s*=\s*["\']([0-9a-f]*)["\']', f.read(), re.M)
    if not want or want.group(1) != pub:
        sys.exit("this key is not the one in version.py (UPDATE_KEY): Biruni would refuse the update")
    version = boot.folder_version(a.source)
    out = os.path.join(a.site, "updates")
    old = os.path.join(out, "manifest.json")
    if os.path.isfile(old):
        with open(old, encoding="utf-8") as f:
            was = json.load(f).get("version", "")
        if boot.parse_version(version) <= boot.parse_version(was):
            sys.exit(f"version {version} is not newer than the published {was}: bump VERSION in version.py first")
    files, new, new_bytes = {}, 0, 0
    for rel, path in sorted(client.files_of(a.source).items()):
        if os.path.basename(path) == boot.MARK:
            continue
        sha, size = client.sha256_file(path), os.path.getsize(path)
        files[rel] = [sha, size]
        blob = os.path.join(out, "files", sha[:2], sha)
        if not os.path.isfile(blob):
            os.makedirs(os.path.dirname(blob), exist_ok=True)
            shutil.copyfile(path, blob)
            new += 1
            new_bytes += size
    manifest = {"format": client.FORMAT, "version": version,
                "published": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "notes": {k: v for k, v in (("ar", a.notes_ar), ("en", a.notes_en)) if v},
                "files": files}
    if a.engine:
        manifest["engine"] = a.engine
    data = json.dumps(manifest, ensure_ascii=False, indent=0, sort_keys=True).encode("utf-8")
    sig = crypto.sign(seed, data).hex()
    assert client.verify(data, sig, pub)
    with open(os.path.join(out, "manifest.json"), "wb") as f:
        f.write(data)
    with open(os.path.join(out, "manifest.sig"), "w", encoding="ascii") as f:
        f.write(sig + "\n")
    with open(os.path.join(out, f"manifest-{version}.json"), "wb") as f:
        f.write(data)
    print(f"version {version}: {len(files)} files, {new} new ({new_bytes / 1e6:.2f} MB) in {out}")


if __name__ == "__main__":
    main()
