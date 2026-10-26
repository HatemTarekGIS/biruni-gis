"""Render Biruni GIS branding assets from the master logo SVG.

Run with the engine's Python (needs PyQt6 + Pillow):
    engine\\bin\\python-qgis.bat scripts\\make_branding.py <plugin dir>

Writes into <plugin dir>/resources/branding/:
    logo-256.png, logo-1024.png, biruni.ico (16..256), splash.png (1200x700)
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from PIL import Image  # noqa: E402
from PyQt6.QtCore import QByteArray, QPointF, QRectF, Qt  # noqa: E402
from PyQt6.QtGui import (QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter, QPen,  # noqa: E402
                         QRadialGradient)
from PyQt6.QtSvg import QSvgRenderer  # noqa: E402

plugin_dir = sys.argv[1]
out = os.path.join(plugin_dir, "resources", "branding")
version = sys.argv[2] if len(sys.argv) > 2 else "0.1"
app = QGuiApplication(sys.argv)
svg = open(os.path.join(out, "logo.svg"), encoding="utf-8").read()
renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))


def render_logo(size):
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p)
    p.end()
    return img


for s in (256, 1024):
    render_logo(s).save(os.path.join(out, f"logo-{s}.png"))

# ICO with the standard Windows sizes, rendered individually (not downscaled).
ico_sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
tmp = []
for s in ico_sizes:
    path = os.path.join(out, f"_ico_{s}.png")
    render_logo(s).save(path)
    tmp.append(path)
big = Image.open(tmp[-1])
big.save(os.path.join(out, "biruni.ico"), sizes=[(s, s) for s in ico_sizes],
         append_images=[Image.open(p) for p in tmp[:-1]])
for p in tmp:
    os.remove(p)

# Splash screen (drawn at 2x for crisp HiDPI display: shown at 600x350).
W, H = 1200, 700
img = QImage(W, H, QImage.Format.Format_ARGB32)
p = QPainter(img)
p.setRenderHint(QPainter.RenderHint.Antialiasing)
p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
bg = QLinearGradient(0, 0, W, H)
bg.setColorAt(0, QColor("#07282A"))
bg.setColorAt(1, QColor("#0D4744"))
p.fillRect(0, 0, W, H, bg)
glow = QRadialGradient(QPointF(W * 0.78, H * 0.05), W * 0.6)
glow.setColorAt(0, QColor(20, 163, 148, 90))
glow.setColorAt(1, QColor(20, 163, 148, 0))
p.fillRect(0, 0, W, H, glow)
# horizon of the Earth
p.setPen(QPen(QColor(255, 255, 255, 60), 3))
p.setBrush(QColor(255, 255, 255, 14))
p.drawEllipse(QPointF(W * 0.5, H + 1500), 2000, 2000)
# line of sight across the scene
p.setPen(QPen(QColor(251, 191, 36, 150), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
p.drawLine(QPointF(118, 238), QPointF(W + 40, 610))

renderer.render(p, QRectF(96, 150, 220, 220))

title = QFont("Segoe UI", 1)
title.setPixelSize(92)
title.setWeight(QFont.Weight.DemiBold)
p.setFont(title)
p.setPen(QColor("#FFFFFF"))
p.drawText(QRectF(356, 150, 820, 120), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "Biruni GIS")

tag = QFont("Segoe UI", 1)
tag.setPixelSize(34)
p.setFont(tag)
p.setPen(QColor(255, 255, 255, 210))
p.drawText(QRectF(360, 262, 820, 50), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
           "Measure, map and analyse the world.")
p.setPen(QColor(251, 191, 36, 235))
p.drawText(QRectF(360, 312, 820, 52), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
           "قِس العالم وارسمه وحلّله")

small = QFont("Segoe UI", 1)
small.setPixelSize(22)
p.setFont(small)
p.setPen(QColor(255, 255, 255, 150))
p.drawText(QRectF(96, H - 92, W - 192, 40), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
           f"Version {version}   ·   Free and open source   ·   Based on QGIS")
p.end()
img.save(os.path.join(out, "splash.png"))
print("branding written to", out)
