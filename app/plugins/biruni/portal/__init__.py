"""A signed portal with no server: a folder any static host can serve (GitHub Pages, a shared folder, a USB stick),
whose index and items are signed by the owner, whose groups are keys, which can follow other portals, and whose
items can expire. See SPEC.md."""


def open_dialog(plugin):
    from .dialog import open_dialog as _open

    return _open(plugin)


def build_hosted_viewer(root=None):
    """build/bridge-page/portal/index.html (for gh-pages). -> path"""
    from .page import build_bridge_viewer

    return build_bridge_viewer(root)
