"""Theme-aware SVG icons (Tabler Icons, MIT - see resources/icons/LICENSE).

Icons are single-colour line SVGs using ``currentColor``; they are recoloured
per theme and rasterised at several sizes so they stay crisp at any DPI.
"""
import os

from qgis.PyQt.QtCore import QByteArray, Qt
from qgis.PyQt.QtGui import QIcon, QImage, QPainter, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer

ICON_DIR = os.path.join(os.path.dirname(__file__), "resources", "icons")
BRANDING_DIR = os.path.join(os.path.dirname(__file__), "resources", "branding")
_SIZES = (16, 20, 24, 28, 32, 40, 48, 64, 96)
_raw = {}
_cache = {}


def _svg(name):
    if name not in _raw:
        path = os.path.join(ICON_DIR, name + ".svg")
        with open(path, encoding="utf-8") as f:
            _raw[name] = f.read().replace('stroke-width="2"', 'stroke-width="1.75"')
    return _raw[name]


def _render(data, size):
    renderer = QSvgRenderer(QByteArray(data.encode("utf-8")))
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p)
    p.end()
    return QPixmap.fromImage(img)


def icon(name, color, disabled=None, active=None):
    """QIcon for ``name`` drawn in ``color`` (+ optional disabled/active colours)."""
    key = (name, color, disabled, active)
    if key in _cache:
        return _cache[key]
    svg = _svg(name)
    ic = QIcon()
    for size in _SIZES:
        ic.addPixmap(_render(svg.replace("currentColor", color), size), QIcon.Mode.Normal)
        if disabled:
            ic.addPixmap(_render(svg.replace("currentColor", disabled), size), QIcon.Mode.Disabled)
        if active:
            ic.addPixmap(_render(svg.replace("currentColor", active), size), QIcon.Mode.Normal, QIcon.State.On)
    _cache[key] = ic
    return ic


def exists(name):
    return os.path.exists(os.path.join(ICON_DIR, name + ".svg"))


def logo_path(kind="svg"):
    return os.path.join(BRANDING_DIR, "logo.svg" if kind == "svg" else "logo-256.png")


def logo_pixmap(size):
    with open(logo_path(), encoding="utf-8") as f:
        return _render(f.read(), size)


def app_icon():
    ico = os.path.join(BRANDING_DIR, "biruni.ico")
    return QIcon(ico) if os.path.exists(ico) else QIcon(logo_path())
