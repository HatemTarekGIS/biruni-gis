"""Updates of Biruni's own code without downloading the whole program.

The publisher (scripts/publish_update.py) puts on the web a list of every file of this folder with its SHA-256,
signed with a key only the publisher holds (Ed25519), and each file once, named by its SHA-256. Here:

  1. ``check`` reads the list and refuses it unless the signature is right and the version is newer.
  2. ``stage`` builds the new copy in the user's folder: a file this computer already has (same SHA-256) is
     copied from the copy that runs; only the others are downloaded, each checked against its SHA-256.
  3. The copy is marked verified and set as ``next``: it runs from the next start (update/boot.py).

Nothing here changes the running program, and nothing reaches the program's own folder.
"""
import datetime as dt
import hashlib
import json
import os
import shutil
import urllib.request

from . import boot

FORMAT = "biruni-update"
SKIP = ("__pycache__",)


class UpdateError(Exception):
    pass


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "BiruniGIS-update", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def files_of(folder):
    """{relative path with /: absolute path} of a copy of the plugin (compiled caches left out)."""
    out = {}
    for d, dirs, names in os.walk(folder):
        dirs[:] = [x for x in dirs if x not in SKIP]
        for n in names:
            if n.endswith((".pyc", ".pyo")):
                continue
            p = os.path.join(d, n)
            out[os.path.relpath(p, folder).replace(os.sep, "/")] = p
    return out


def verify(manifest_bytes, sig_hex, public_hex):
    from ..secure import crypto

    try:
        return crypto.verify(bytes.fromhex(public_hex), bytes.fromhex(sig_hex.strip()), manifest_bytes)
    except (ValueError, TypeError):
        return False


def check(base_url, public_hex, running_version, engine_version=None):
    """The signed list of the newest version when it is newer than ``running_version`` and fits this engine,
    else None. UpdateError when the list cannot be trusted."""
    base = base_url.rstrip("/") + "/"
    data = fetch(base + "manifest.json")
    sig = fetch(base + "manifest.sig").decode("ascii", "replace")
    if not verify(data, sig, public_hex):
        raise UpdateError("the update list is not signed by Biruni's key")
    m = json.loads(data.decode("utf-8"))
    if m.get("format") != FORMAT or not isinstance(m.get("files"), dict):
        raise UpdateError("not an update list")
    if boot.parse_version(m.get("version")) <= boot.parse_version(running_version):
        return None
    need = m.get("engine")
    if need and engine_version and boot.parse_version(engine_version) < boot.parse_version(need):
        m["installer"] = True    # this version needs a newer engine: the whole program, not an update
    return m


def plan(manifest, running_dir):
    """What an update needs: (files to copy from the running copy, files to download)."""
    have = files_of(running_dir)
    copy, get = {}, {}
    for rel, (sha, size) in manifest["files"].items():
        if ".." in rel.split("/") or rel.startswith("/") or ":" in rel:
            raise UpdateError(f"a bad file name in the update list: {rel}")
        p = have.get(rel)
        if p is not None and os.path.getsize(p) == size and sha256_file(p) == sha:
            copy[rel] = p
        else:
            get[rel] = (sha, size)
    return copy, get


def stage(manifest, base_url, running_dir, updates=None, progress=None, cancelled=lambda: False):
    """Build the new copy and set it to run from the next start.
    -> {"version", "downloaded", "bytes", "copied"}"""
    updates = updates or boot.root()
    if not updates:
        raise UpdateError("not an installed Biruni")
    version = manifest["version"]
    copy, get = plan(manifest, running_dir)
    tmp = os.path.join(updates, f"_staging-{version}")
    shutil.rmtree(tmp, ignore_errors=True)
    dest = os.path.join(tmp, "biruni")
    os.makedirs(dest)
    base = base_url.rstrip("/") + "/"
    done = total = 0
    try:
        for rel, src in copy.items():
            p = os.path.join(dest, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            shutil.copy2(src, p)
        for i, (rel, (sha, size)) in enumerate(get.items(), 1):
            if cancelled():
                raise UpdateError("cancelled")
            data = fetch(f"{base}files/{sha[:2]}/{sha}", timeout=120)
            if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
                raise UpdateError(f"{rel} did not arrive whole")
            p = os.path.join(dest, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
            done += 1
            total += len(data)
            if progress:
                progress(i, len(get))
        if boot.folder_version(dest) != version:
            raise UpdateError("the copy does not say the version of the list")
        with open(os.path.join(dest, boot.MARK), "w", encoding="utf-8") as f:
            json.dump({"version": version, "verified": True, "files": len(manifest["files"]),
                       "staged": dt.datetime.now().isoformat(timespec="seconds")}, f)
        final = os.path.join(updates, version)
        shutil.rmtree(final, ignore_errors=True)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    state = boot.read_state(updates)
    state["next"] = version
    state["bad"] = [v for v in state.get("bad") or [] if v != version]   # published again: try it again
    boot.write_state(updates, state)
    return {"version": version, "downloaded": done, "bytes": total, "copied": len(copy)}


def staged_version():
    updates = boot.root()
    return (boot.read_state(updates).get("next") if updates else None) or None


def is_bad(version):
    updates = boot.root()
    return bool(updates) and version in (boot.read_state(updates).get("bad") or [])
