"""Living paper: a printed map that talks. A QR code on the print layout opens the same map area on the phone,
with the reader's own position and how far and which way the map is - no server anywhere (the map travels in the
address itself, after "#")."""
from .codec import MAX_CHARS, PAGE_URL, decode, encode  # noqa: F401
from .page import public_page  # noqa: F401


def add_live_qr(*args, **kwargs):
    """See layout.add_live_qr (imported on use: it needs QGIS)."""
    from .layout import add_live_qr as run

    return run(*args, **kwargs)
