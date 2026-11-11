"""Theme-aware SVG icons (Tabler Icons, MIT - see resources/icons/LICENSE).

Icons are single-colour line SVGs using ``currentColor``; they are recoloured
per theme and rasterised at several sizes so they stay crisp at any DPI.
"""
import os

from qgis.PyQt.QtCore import QByteArray, QSize, Qt
from qgis.PyQt.QtGui import QIcon, QIconEngine, QImage, QPainter, QPixmap
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.PyQt.QtWidgets import QApplication, QStyleOption

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


class _SvgEngine(QIconEngine):
    """Draws an icon only when Qt shows it, at the size it is shown, and keeps that one picture.
    (Drawing every icon at nine sizes up front cost seconds at start-up and many MB of pictures of
    buttons on ribbon tabs that are never opened.)"""

    def __init__(self, svgs):
        super().__init__()
        self.svgs = svgs  # (mode, state) -> recoloured SVG text
        self.pixmaps = {}

    def pixmap(self, size, mode, state):
        side = max(1, min(size.width(), size.height()))
        key = (side, mode, state)
        pm = self.pixmaps.get(key)
        if pm is None:
            normal = self.svgs[(QIcon.Mode.Normal, QIcon.State.Off)]
            svg = self.svgs.get((mode, state)) or self.svgs.get((mode, QIcon.State.Off))
            pm = _render(svg or self.svgs.get((QIcon.Mode.Normal, state)) or normal, side)
            if svg is None and mode == QIcon.Mode.Disabled and QApplication.instance() is not None:
                pm = QApplication.style().generatedIconPixmap(mode, pm, QStyleOption())  # Qt's greyed look
            self.pixmaps[key] = pm
        return pm

    def paint(self, painter, rect, mode, state):
        ratio = painter.device().devicePixelRatioF() if painter.device() is not None else 1.0
        painter.drawPixmap(rect, self.pixmap(QSize(round(rect.width() * ratio), round(rect.height() * ratio)),
                                             mode, state))

    def actualSize(self, size, mode, state):  # noqa: N802
        return size

    def availableSizes(self, mode=QIcon.Mode.Normal, state=QIcon.State.Off):  # noqa: N802
        return [QSize(s, s) for s in _SIZES]

    def clone(self):
        return _SvgEngine(self.svgs)


def forget_pictures():
    """Drop the drawn pictures of every icon (they are drawn again when shown) - frees memory."""
    for engine in _engines.values():
        engine.pixmaps.clear()


_engines = {}  # the engine of each cached icon (its pictures can be dropped)


def icon(name, color, disabled=None, active=None):
    """QIcon for ``name`` drawn in ``color`` (+ optional disabled/active colours)."""
    key = (name, color, disabled, active)
    if key in _cache:
        return _cache[key]
    svg = _svg(name)
    svgs = {(QIcon.Mode.Normal, QIcon.State.Off): svg.replace("currentColor", color)}
    if disabled:
        svgs[(QIcon.Mode.Disabled, QIcon.State.Off)] = svg.replace("currentColor", disabled)
    if active:
        svgs[(QIcon.Mode.Normal, QIcon.State.On)] = svg.replace("currentColor", active)
    engine = _SvgEngine(svgs)
    ic = QIcon(engine)
    _cache[key] = ic
    _engines[key] = engine
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
