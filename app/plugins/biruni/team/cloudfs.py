"""Cloud-synced filesystem detection for shared room folders (pure Python, no Qt/QGIS imports)."""
import os

from ..field_photos import drive

_RECALL_DATA = 0x400000      # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
_RECALL_OPEN = 0x40000       # FILE_ATTRIBUTE_RECALL_ON_OPEN
_OFFLINE = 0x1000            # FILE_ATTRIBUTE_OFFLINE
_PINNED = 0x80000            # FILE_ATTRIBUTE_PINNED
_UNPINNED = 0x100000         # FILE_ATTRIBUTE_UNPINNED
_PLACEHOLDER_ATTRS = _RECALL_DATA | _RECALL_OPEN | _OFFLINE
_CLOUD_ATTRS = _PLACEHOLDER_ATTRS | _PINNED | _UNPINNED
_CLOUD_REPARSE_MASK = 0xFFFF0FFF
_CLOUD_REPARSE_TAG = 0x9000001A


def volume_of(path, _info=None):
    """Return (mount, label, fsname) for ``path``, or ("", "", "") on failure or off Windows."""
    if _info is not None:
        return _info(path) if callable(_info) else tuple(_info)
    if os.name != "nt" or not path:
        return ("", "", "")
    import ctypes

    try:
        real = os.path.realpath(str(path))
        k32 = ctypes.windll.kernel32
        mbuf = ctypes.create_unicode_buffer(max(261, len(real) + 16))
        if not k32.GetVolumePathNameW(real, mbuf, len(mbuf)):
            return ("", "", "")
        mount = mbuf.value
        lbuf = ctypes.create_unicode_buffer(261)
        fbuf = ctypes.create_unicode_buffer(261)
        if not k32.GetVolumeInformationW(mount, lbuf, 261, None, None, None, fbuf, 261):
            return ("", "", "")
        return (mount, lbuf.value, fbuf.value)
    except OSError:
        return ("", "", "")


def _is_cloud_files(path):
    """True when ``path`` or an ancestor carries Windows Cloud Files attributes or reparse tag."""
    if os.name != "nt" or not path:
        return False
    cur = os.path.realpath(str(path))
    while cur:
        try:
            st = os.stat(cur)
            attrs = getattr(st, "st_file_attributes", 0)
            tag = getattr(st, "st_reparse_tag", 0)
            if (attrs & _CLOUD_ATTRS) or ((tag & _CLOUD_REPARSE_MASK) == _CLOUD_REPARSE_TAG):
                return True
        except OSError:
            pass
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return False


def kind(path, _info=None):
    """Return 'gdrive-stream', 'cloud-files', or 'plain' for ``path``."""
    _mount, label, _fs = volume_of(path, _info)
    if "google drive" in str(label or "").lower():
        return "gdrive-stream"
    if _is_cloud_files(path):
        return "cloud-files"
    return "plain"


def placeholder(path, _attrs=None):
    """True if ``path`` is an offline cloud placeholder, None on gdrive-stream, else False."""
    if kind(path) == "gdrive-stream":
        return None
    if _attrs is not None:
        attrs = _attrs(path) if callable(_attrs) else int(_attrs)
        return bool(attrs & _PLACEHOLDER_ATTRS)
    try:
        attrs = getattr(os.stat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & _PLACEHOLDER_ATTRS)


def find_room(tag, roots):
    """Return parent folders under ``roots`` that hold 'biruni-room-<tag>'."""
    name = "biruni-room-" + str(tag)[:16]
    hits = drive.named_folders(roots, name)
    return list(dict.fromkeys(os.path.dirname(h) for h in hits))
