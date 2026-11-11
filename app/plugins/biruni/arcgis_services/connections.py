"""Saved connections to ArcGIS Server, Portal for ArcGIS and ArcGIS Online.

The address, the sign-in kind and the user name are kept in the settings; passwords and API keys are kept
encrypted for this Windows user (DPAPI), never as plain text. On other systems they are not kept at all and
are asked for again in each session."""
import base64
import sys

from qgis.core import QgsSettings

from .rest import AUTH_APIKEY, AUTH_NONE, AUTH_OAUTH, AUTH_TOKEN, Connection

_ROOT = "biruni/arcgis-services/connections"
_SESSION = {}          # name -> secrets typed this session (other systems; and tokens)


# ---- DPAPI (Windows): encrypted for the current user only -------------------------------------------------------------
def _dpapi(data, encrypt):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    crypt = ctypes.windll.crypt32
    fn = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):   # CRYPTPROTECT_UI_FORBIDDEN
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def can_store_secrets():
    return sys.platform == "win32"


def protect(text):
    """Text encrypted for this Windows user, as base64 ("" when it cannot be kept safely)."""
    if not text or not can_store_secrets():
        return ""
    return "dpapi:" + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")


def unprotect(stored):
    if not stored or not stored.startswith("dpapi:") or not can_store_secrets():
        return ""
    try:
        return _dpapi(base64.b64decode(stored[6:]), False).decode("utf-8")
    except (ValueError, OSError, UnicodeDecodeError):
        return ""


# ---- the list ------------------------------------------------------------------------------------------------------
def _key(name):
    return f"{_ROOT}/{name.replace('/', '_')}"


def names():
    s = QgsSettings()
    s.beginGroup(_ROOT)
    out = sorted(s.childGroups(), key=str.lower)
    s.endGroup()
    return out


def load(name):
    """Saved connection (secrets decrypted) or None."""
    s = QgsSettings()
    k = _key(name)
    url = s.value(k + "/url", "")
    if not url:
        return None
    c = Connection(name=name, url=url, auth=s.value(k + "/auth", AUTH_NONE), username=s.value(k + "/username", ""),
                   client_id=s.value(k + "/client_id", ""), referer=s.value(k + "/referer", ""),
                   verify_ssl=str(s.value(k + "/verify_ssl", "true")).lower() != "false")
    c.password = unprotect(s.value(k + "/password", "")) or _SESSION.get(name, {}).get("password", "")
    c.api_key = unprotect(s.value(k + "/api_key", "")) or _SESSION.get(name, {}).get("api_key", "")
    sess = _SESSION.get(name, {})
    c.token, c.token_expires, c.refresh_token = sess.get("token", ""), sess.get("expires", 0.0), sess.get("refresh", "")
    return c


def save(conn, keep_password=True):
    """Store a connection; the password / API key are encrypted (or kept for this session only)."""
    s = QgsSettings()
    k = _key(conn.name)
    for attr in ("url", "auth", "username", "client_id", "referer"):
        s.setValue(f"{k}/{attr}", getattr(conn, attr))
    s.setValue(k + "/verify_ssl", "true" if conn.verify_ssl else "false")
    secret_pw = protect(conn.password) if keep_password and conn.auth == AUTH_TOKEN else ""
    secret_key = protect(conn.api_key) if conn.auth == AUTH_APIKEY else ""
    s.setValue(k + "/password", secret_pw)
    s.setValue(k + "/api_key", secret_key)
    sess = _SESSION.setdefault(conn.name, {})
    if conn.password and not secret_pw:
        sess["password"] = conn.password
    if conn.api_key and not secret_key:
        sess["api_key"] = conn.api_key
    remember_token(conn)


def remember_token(conn):
    """Tokens live for this session only (never written to the settings)."""
    if conn.auth in (AUTH_TOKEN, AUTH_OAUTH) and conn.token:
        _SESSION.setdefault(conn.name, {}).update(token=conn.token, expires=conn.token_expires,
                                                  refresh=conn.refresh_token)


def delete(name):
    s = QgsSettings()
    s.remove(_key(name))
    _SESSION.pop(name, None)


def find_for_url(url):
    """The saved connection whose address starts the given service URL (longest match), or None."""
    from .rest import services_root

    root = services_root(url).lower()
    best = None
    for n in names():
        c = load(n)
        if c and root.startswith(services_root(c.url).lower()):
            if best is None or len(c.url) > len(best.url):
                best = c
    return best


def stored_as_plain_text(name):
    """True if any secret of this connection is in the settings unencrypted (it never should be)."""
    s = QgsSettings()
    k = _key(name)
    return any(v and not str(v).startswith("dpapi:") for v in (s.value(k + "/password", ""),
                                                               s.value(k + "/api_key", "")))
