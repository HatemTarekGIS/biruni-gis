"""The office's one restic password: made once, kept under DPAPI in <cert>\\backup_pw.dat.

It also travels inside the v2 key backup (body["backup_pw"], field_photos/handover.py), so a new PC can open the
repository. Never put it in argv, a log or state.json.
"""
import os
import secrets
import time

FILE = "backup_pw.dat"


def path(cert_folder):
    return os.path.join(cert_folder, FILE)


def load_password(cert_folder):
    """The password, or "" when there is none or it cannot be unprotected (another Windows account)."""
    from ..field_photos.tunnel import unprotect

    try:
        with open(path(cert_folder), encoding="ascii") as f:
            return unprotect(f.read().strip())
    except OSError:
        return ""


def _write(cert_folder, pw):
    from ..field_photos.tunnel import protect

    os.makedirs(cert_folder, exist_ok=True)
    p = path(cert_folder)
    with open(p + ".tmp", "w", encoding="ascii") as f:
        f.write(protect(pw))
    os.replace(p + ".tmp", p)


def ensure_password(cert_folder):
    """(password, created). An existing file that cannot be read is never written over: it is moved aside first."""
    pw = load_password(cert_folder)
    if pw:
        return pw, False
    p = path(cert_folder)
    if os.path.isfile(p):
        os.replace(p, p + ".unreadable-" + time.strftime("%Y%m%d-%H%M%S"))
    pw = secrets.token_urlsafe(24)
    _write(cert_folder, pw)
    return pw, True


def install_password(cert_folder, pw):
    """Make ``pw`` the office's password (from a key backup). A different one already here is kept as .old
    (repositories made with it still open). True when something was written."""
    cur = load_password(cert_folder)
    if cur == pw:
        return False
    p = path(cert_folder)
    if cur and os.path.isfile(p):
        os.replace(p, p + "." + time.strftime("%Y%m%d-%H%M%S") + ".old")
    _write(cert_folder, pw)
    return True
