"""The door check («حجر الباب»): this PC never hands out a link or QR code to a phone page that Biruni did not
release.

Every page phones open (the bridge's p/, the dashboard's d/, the live map's s/, …) is a static file on the
vendor's GitHub Pages site. If that site were changed - a stolen GitHub account, a wrong push - every office
would hand its phones a changed page, silently. So:

  * When the pages are built (scripts/build_bridge_page.py), ``write_release`` lists every file of the site
    with its SHA-256 in ``release.json`` and signs it (``release.sig``, Ed25519 hex) with the same key that signs
    Biruni's updates (_secrets/update-signing.key; its public half is version.UPDATE_KEY, shipped in Biruni).
  * Before a link to such a page is shown, ``check_door`` fetches the live page and the live ``release.json``,
    checks the signature against the shipped key and the page's SHA-256 against the list.
    ok -> the link is shown; changed / not signed -> it is not, unless the owner says "show anyway";
    offline -> it is shown, marked "not checked" (phones that already opened the page use their own copy).

What it does NOT do (say so): it checks the page at the moment this PC shows a link. A phone that already
cached a bad page, or that opens the page later after a new change, is not protected by this check; a page
served differently to one phone than to this PC is not seen; and whoever holds the update-signing key can sign a
bad page (protecting that key from one person is idea G12, which was dropped).
"""
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

SITE = "https://biruni-gis.com/"
FORMAT = "biruni-release"
RELEASE, SIG = "release.json", "release.sig"
TTL = 300                 # a verdict is reused this many seconds
TIMEOUT = 6
SKIP_DIRS = ("__pycache__", ".git")

_cache = {}               # (site, rel) -> (monotonic time, verdict)
_rel_cache = {}           # site -> (monotonic time, release dict or exception text)
_lock = threading.Lock()


# ---- the release list (build side) --------------------------------------------------------------------------------
def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def list_files(folder):
    out = {}
    for d, dirs, names in os.walk(folder):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        for n in sorted(names):
            rel = os.path.relpath(os.path.join(d, n), folder).replace(os.sep, "/")
            if rel in (RELEASE, SIG):
                continue
            with open(os.path.join(d, n), "rb") as f:
                out[rel] = sha256_bytes(f.read())
    return out


def write_release(folder, seed, version=None):
    """Write folder/release.json (every file and its SHA-256) and folder/release.sig. -> the release dict"""
    from . import crypto

    rel = {"format": FORMAT, "version": version or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "files": list_files(folder)}
    data = json.dumps(rel, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
    with open(os.path.join(folder, RELEASE), "wb") as f:
        f.write(data)
    with open(os.path.join(folder, SIG), "w", encoding="ascii") as f:
        f.write(crypto.sign(seed, data).hex())
    return rel


def sign_folder(folder, key_path):
    """Sign a built site with the update-signing key file (hex seed). -> the release dict"""
    with open(key_path, encoding="ascii") as f:
        seed = bytes.fromhex(f.read().strip())
    return write_release(folder, seed)


# ---- the check (PC side) ------------------------------------------------------------------------------------------
def shipped_key():
    try:
        from ..version import UPDATE_KEY

        return UPDATE_KEY
    except Exception:  # noqa: BLE001
        return ""


def rel_of(url, site=SITE):
    """The file a page URL serves, relative to the site ("p/index.html"), or None when not on the site."""
    u = str(url or "").split("#", 1)[0].split("?", 1)[0]
    if not u.startswith(site):
        return None
    rel = u[len(site):]
    if rel == "" or rel.endswith("/"):
        rel += "index.html"
    return rel


def _fetch(url, timeout):
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(url + sep + "door=" + secrets.token_hex(6),
                                 headers={"User-Agent": "BiruniGIS-door", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _verify(data, sig_hex, public_hex):
    from . import crypto

    try:
        return bool(public_hex) and crypto.verify(bytes.fromhex(public_hex), bytes.fromhex(sig_hex.strip()), data)
    except (ValueError, TypeError):
        return False


def _release(site, public_hex, fetch, timeout):
    """The site's signed release list -> dict; raises _Unsigned / _Offline."""
    try:
        data = fetch(site + RELEASE, timeout)
        sig = fetch(site + SIG, timeout).decode("ascii", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _Unsigned("the site has no signed release list") from e
        raise _Offline(str(e)) from e
    except Exception as e:  # noqa: BLE001 - no network, DNS, timeout
        raise _Offline(str(e)) from e
    if not _verify(data, sig, public_hex):
        raise _Unsigned("the release list is not signed by Biruni's key")
    try:
        rel = json.loads(data.decode("utf-8"))
    except ValueError as e:
        raise _Unsigned("the release list cannot be read") from e
    if rel.get("format") != FORMAT or not isinstance(rel.get("files"), dict):
        raise _Unsigned("not a release list")
    return rel


class _Unsigned(Exception):
    pass


class _Offline(Exception):
    pass


def check_door(page_url, site=SITE, public_hex=None, timeout=TIMEOUT, fetch=None, fresh=False):
    """Is the page this link opens one that Biruni released?
    -> {"state": "ok" | "changed" | "unsigned" | "offline" | "other", "page": rel, "why": text, "version": …}
    "other": the link is not on the vendor site (a page of this PC, a LAN address): nothing to check."""
    rel = rel_of(page_url, site)
    if rel is None:
        return {"state": "other", "page": None, "why": "", "version": None}
    key = (site, rel)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and not fresh and now - hit[0] < TTL:
            return dict(hit[1])
    fetch = fetch or _fetch
    public_hex = shipped_key() if public_hex is None else public_hex
    v = {"state": "ok", "page": rel, "why": "", "version": None}
    try:
        with _lock:
            rc = _rel_cache.get(site)
        if rc and not fresh and now - rc[0] < TTL:
            rel_list = rc[1]
        else:
            rel_list = _release(site, public_hex, fetch, timeout)
            with _lock:
                _rel_cache[site] = (now, rel_list)
        v["version"] = rel_list.get("version")
        try:
            page = fetch(site + rel, timeout)
        except urllib.error.HTTPError as e:
            raise _Offline(str(e)) if e.code != 404 else _Unsigned("the page is not on the site") from e
        except Exception as e:  # noqa: BLE001
            raise _Offline(str(e)) from e
        want = rel_list["files"].get(rel)
        if want is None:
            v.update(state="changed", why="the page is not in the signed release list")
        elif want != sha256_bytes(page):
            v.update(state="changed", why="the page differs from the released one")
    except _Unsigned as e:
        v.update(state="unsigned", why=str(e))
    except _Offline as e:
        v.update(state="offline", why=str(e))
    if v["state"] != "offline":
        with _lock:
            _cache[key] = (now, dict(v))
    return v


def prewarm(page_url):
    """Start the check in the background so that the link shows at once later."""
    threading.Thread(target=lambda: check_door(page_url), daemon=True).start()


def forget():
    with _lock:
        _cache.clear()
        _rel_cache.clear()


# ---- the gate (Qt): used where a link or a QR code is shown ---------------------------------------------------------
LAST = {}   # page rel -> last verdict (the panels may show "not checked")


def door_ok(parent, page_url, wait=8.0, check=None):
    """True when the link may be shown. Checks in a thread (the window keeps painting, at most ``wait`` s);
    a changed or unsigned page asks the owner. Offline or not on the vendor site -> True."""
    box = {}
    fn = check or check_door

    def run():
        try:
            box["v"] = fn(page_url)
        except Exception as e:  # noqa: BLE001 - a bug here must never hide the owner's links
            box["v"] = {"state": "offline", "page": None, "why": str(e), "version": None}

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        from qgis.PyQt.QtWidgets import QApplication

        end = time.monotonic() + wait
        while t.is_alive() and time.monotonic() < end:
            QApplication.processEvents()
            t.join(0.03)
    except Exception:  # noqa: BLE001 - no Qt (tests)
        t.join(wait)
    v = box.get("v") or {"state": "offline", "page": None, "why": "the check took too long", "version": None}
    LAST[v.get("page")] = v
    if v["state"] in ("ok", "other", "offline"):
        return True
    return ask_owner(parent, v)


def ask_owner(parent, v):
    try:
        from qgis.PyQt.QtWidgets import QMessageBox

        from ..i18n import tr
    except Exception:  # noqa: BLE001
        return False
    if v["state"] == "changed":
        text = tr("The phone page on Biruni's site ({page}) is not the one Biruni released. Someone may have changed "
                  "the site. The link and QR code are not shown, so that no phone opens a page that may steal its "
                  "data.").format(page=v.get("page") or "")
    else:
        text = tr("The phone pages on Biruni's site are not signed by Biruni's key ({why}). The link and QR code "
                  "are not shown, so that no phone opens a page that may steal its data.").format(why=v.get("why", ""))
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(tr("The phone page could not be trusted"))
    box.setText(text)
    anyway = box.addButton(tr("Show anyway (I understand)"), QMessageBox.ButtonRole.DestructiveRole)
    box.addButton(tr("Don't show"), QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is anyway
