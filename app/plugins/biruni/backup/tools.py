r"""Command-line tools the backup needs, pinned: where they are, and how one is fetched when it is not shipped.

The release puts restic in engine\tools\restic\ (scripts/engine-tools.txt); a PC updated by self-update (which stages
the plugin folder only) downloads the same official zip once, checks its SHA-256, takes the one member out, checks the
program's own SHA-256 and keeps it under <settings>\biruni\tools\<name>\<version>\.  Never "latest", never a self-update,
and the program is re-hashed before every run (verified).

Syncthing (MPL-2.0, run with --no-upgrade) and rclone (MIT) are downloaded on demand only, pinned by their official
release SHA-256 hashes.
"""
import hashlib
import os
import urllib.request
import zipfile

PLACEHOLDER = "PLACEHOLDER-pin-not-set"

# zip_sha256: the official Windows amd64 zip's line in the release's SHA256SUMS.
# exe_sha256: the program inside it, measured once on a download whose zip hash was checked first.
PINNED = {
    "restic": {
        "version": "0.20.0",
        "url": "https://github.com/restic/restic/releases/download/v0.20.0/restic_0.20.0_windows_amd64.zip",
        "zip_sha256": "bba9061c3dce081701ab353ec8cb73a78be73b71a538aad560f2d1067602b7fe",
        "exe_sha256": "2683eb2b061f11568b9132ac0242f183ba4b3a0ed7630f1186a57e889c82a0e2",
        "member": "restic_0.20.0_windows_amd64.exe",
    },
    "syncthing": {
        "version": "2.1.7",
        "url": "https://github.com/syncthing/syncthing/releases/download/v2.1.7/syncthing-windows-amd64-v2.1.7.zip",
        "zip_sha256": "3ce7da770f546a8d02b65b2e7ee31f2a3b8b7c3f990ede045590cca4172ecfe1",
        "exe_sha256": "cd9aea20afda2b9bdba5aff2e6ac964ed5c2e741ed4ceb48ad9e1be096ca7891",
        "member": "syncthing-windows-amd64-v2.1.7/syncthing.exe",
    },
    "rclone": {
        "version": "1.76.0",
        "url": "https://github.com/rclone/rclone/releases/download/v1.76.0/rclone-v1.76.0-windows-amd64.zip",
        "zip_sha256": "8b4fd2737ade4e70c057ef4a49deb7cffe94b0fb9d7587c189d149823f21a1dc",
        "exe_sha256": "acd80683e23151e93bbe0d6ffa74d26e542ec510332f630beecb9944f8d1a623",
        "member": "rclone-v1.76.0-windows-amd64/rclone.exe",
    },
}


class ToolError(Exception):
    """A tool that cannot be found, downloaded or trusted (the message says why, in English, for the log)."""


def _pin(name, pins=None):
    ent = (PINNED if pins is None else pins).get(name)
    if not ent:
        raise ToolError("unknown tool: " + str(name))
    return ent


def is_placeholder(ent):
    return any(not str(ent.get(k) or "") or PLACEHOLDER in str(ent.get(k)) or str(ent.get(k)).startswith("@")
               for k in ("zip_sha256", "exe_sha256"))


def exe_name(ent):
    return os.path.basename(ent["member"])


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def settings_tools_dir():
    try:
        from qgis.core import QgsApplication

        return os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni", "tools")
    except Exception:  # noqa: BLE001 - plain Python (tests, scripts)
        return os.path.join(os.path.expanduser("~"), ".biruni", "tools")


def _engine_tools_dir():
    try:
        from ..external_tools import engine_root

        return os.path.join(engine_root(), "tools")
    except Exception:  # noqa: BLE001
        return ""


def _candidates(name, ent, tools_dir=None):
    """Folders to look in, best first: a developer's BIRUNI_TOOLS, the engine, the downloaded copy."""
    out = []
    env = os.environ.get("BIRUNI_TOOLS")
    if env:
        out.append(os.path.join(env, name))
    eng = _engine_tools_dir()
    if eng:
        out.append(os.path.join(eng, name))
    out.append(os.path.join(tools_dir or settings_tools_dir(), name, ent["version"]))
    return out


def verified(path, name, pins=None):
    """The program at ``path`` when its SHA-256 is the pinned one; ToolError otherwise (checked before every run)."""
    ent = _pin(name, pins)
    if is_placeholder(ent):
        raise ToolError(name + ": the pinned hashes are not set; refusing to run it")
    try:
        got = sha256_file(path)
    except OSError as e:
        raise ToolError(f"{name}: cannot read {path}: {e}") from e
    if got != ent["exe_sha256"].lower():
        raise ToolError(f"{name}: {path} does not match the pinned SHA-256; refusing to run it")
    return path


def find(name, pins=None, tools_dir=None):
    """The full path of a tool that is here and matches its pin, or None when it is not here.
    ToolError for a pin that is a placeholder, or for a copy that does not match its pin."""
    ent = _pin(name, pins)
    if is_placeholder(ent):
        raise ToolError(name + ": the pinned hashes are not set; refusing to look for it")
    for d in _candidates(name, ent, tools_dir):
        for fn in (exe_name(ent), name + ".exe", name):
            p = os.path.join(d, fn)
            if os.path.isfile(p):
                return verified(p, name, pins)
    return None


def download(name, progress=None, cancelled=None, pins=None, tools_dir=None, opener=None):
    """Fetch the pinned zip over HTTPS, check it, take the one member out, check it, put it in place.
    Returns the program's path. ToolError for a bad hash (nothing is left behind), a cancel or a network error."""
    ent = _pin(name, pins)
    if is_placeholder(ent):
        raise ToolError(name + ": the pinned hashes are not set; refusing to download it")
    if not str(ent["url"]).startswith("https://"):
        raise ToolError(name + ": the pinned address is not https")
    dest_dir = os.path.join(tools_dir or settings_tools_dir(), name, ent["version"])
    os.makedirs(dest_dir, exist_ok=True)
    zpart = os.path.join(dest_dir, "download.zip.part")
    xpart = os.path.join(dest_dir, exe_name(ent) + ".part")
    final = os.path.join(dest_dir, exe_name(ent))

    def _clean():
        for p in (zpart, xpart):
            try:
                os.remove(p)
            except OSError:
                pass

    try:
        h = hashlib.sha256()
        with (opener or urllib.request.urlopen)(ent["url"], timeout=60) as r, open(zpart, "wb") as f:
            total = int(getattr(r, "length", 0) or 0)
            done = 0
            while True:
                if cancelled is not None and cancelled():
                    raise ToolError("cancelled")
                buf = r.read(1 << 18)
                if not buf:
                    break
                h.update(buf)
                f.write(buf)
                done += len(buf)
                if progress is not None:
                    progress(done, total)
        if h.hexdigest() != ent["zip_sha256"].lower():
            raise ToolError(name + ": the downloaded zip does not match the pinned SHA-256")
        try:
            with zipfile.ZipFile(zpart) as z:
                try:
                    info = z.getinfo(ent["member"])
                except KeyError as e:
                    raise ToolError(name + ": the zip has no " + ent["member"]) from e
                hx = hashlib.sha256()
                with z.open(info) as a, open(xpart, "wb") as b:
                    for chunk in iter(lambda: a.read(1 << 20), b""):
                        hx.update(chunk)
                        b.write(chunk)
        except zipfile.BadZipFile as e:
            raise ToolError(name + ": the download is not a zip") from e
        if hx.hexdigest() != ent["exe_sha256"].lower():
            raise ToolError(name + ": the program in the zip does not match the pinned SHA-256")
        os.replace(xpart, final)
        return final
    except (OSError, ValueError) as e:
        raise ToolError(f"{name}: download failed: {e}") from e
    finally:
        _clean()
