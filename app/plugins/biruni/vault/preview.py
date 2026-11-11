"""Quick look (a small map of a file without adding it to the project) and the properties card of a Vault item."""
import datetime
import html
import os

from qgis.core import QgsProject, QgsRasterLayer, QgsRectangle, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout,
                                 QWidget)

from ..i18n import format_number, tr
from .kinds import kind_name, tint


def make_layer(entry, name=None):
    """A map layer for an item (not added to any project), or None."""
    name = name or entry.title
    uri = entry.uri or entry.path
    if not uri:
        return None
    if entry.layer_type == "raster":
        lyr = QgsRasterLayer(uri, name, entry.provider or "gdal")
    elif entry.layer_type == "vector":
        if entry.table and "|layername=" not in uri and entry.provider in ("", "ogr"):
            uri = f"{uri}|layername={entry.table}"
        lyr = QgsVectorLayer(uri, name, entry.provider or "ogr")
    else:
        return None
    return lyr if lyr.isValid() else None


def human_size(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def date_text(t):
    if not t:
        return ""
    try:
        d = datetime.datetime.fromtimestamp(t)
    except (TypeError, ValueError, OSError):
        return ""
    return d.strftime("%Y-%m-%d %H:%M")


def dialog_qss(t):
    return f"""
#VaultDialog {{ background: {t['window']}; }}
#VaultDialog QLabel#VaultTitle {{ font-size: 13pt; font-weight: 700; color: {t['text']}; }}
#VaultDialog QLabel#VaultSub {{ color: {t['text_dim']}; }}
#VaultDialog QTextBrowser {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 10px;
    padding: 6px; color: {t['text']}; }}
#VaultDialog QPushButton {{ border: 1px solid {t['border']}; border-radius: 7px; padding: 6px 14px;
    background: {t['button']}; color: {t['text']}; }}
#VaultDialog QPushButton:hover {{ background: {t['hover']}; }}
#VaultDialog QPushButton#VaultPrimary {{ background: {t['accent']}; color: {t['accent_text']}; border: none;
    font-weight: 600; }}
#VaultDialog QPushButton#VaultPrimary:hover {{ background: {t['accent_hover']}; }}
#VaultDialog QFrame#VaultMapFrame {{ border: 1px solid {t['border']}; border-radius: 10px; background: {t['base']}; }}
"""


def facts_rows(entry):
    """[(label, value)] of an item's facts."""
    f = entry.facts
    rows = []
    k = kind_name(entry.kind)
    if k:
        rows.append((tr("Kind"), k))
    layers = f.get("layers") or []
    if f.get("count") is not None:
        rows.append((tr("Features"), format_number(f["count"], 0)))
    elif len(layers) == 1 and layers[0].get("count") is not None:
        rows.append((tr("Features"), format_number(layers[0]["count"], 0)))
    if len(layers) > 1:
        rows.append((tr("Layers"), ", ".join(ly["name"] for ly in layers)))
    geom = f.get("geometry") or (layers[0].get("geometry") if len(layers) == 1 else "")
    if geom:
        rows.append((tr("Geometry"), geom))
    if f.get("width"):
        rows.append((tr("Pixels"), f"{f['width']} × {f['height']} · {f.get('bands', 1)} " + tr("bands")))
    crs = f.get("crs") or (layers[0].get("crs") if len(layers) == 1 else "")
    crs_name = f.get("crs_name") or (layers[0].get("crs_name") if len(layers) == 1 else "")
    if crs:
        rows.append((tr("Coordinate system"), f"{crs} - {crs_name}" if crs_name and crs_name != crs else crs))
    if f.get("size") is not None:
        rows.append((tr("Size"), human_size(f["size"])))
    if f.get("mtime"):
        rows.append((tr("Last changed"), date_text(f["mtime"])))
    if f.get("encoding"):
        rows.append((tr("Encoding"), f["encoding"]))
    if entry.path:
        rows.append((tr("File"), entry.path))
    elif entry.uri:
        rows.append((tr("Source"), entry.uri))
    return rows


def facts_html(entry, t, used=None):
    rows = "".join(f"<tr><td style='color:{t['text_dim']};padding:3px 14px 3px 0'>{html.escape(a)}</td>"
                   f"<td style='padding:3px 0'>{html.escape(str(b))}</td></tr>" for a, b in facts_rows(entry))
    out = [f"<table>{rows}</table>"]
    if entry.badges:
        pills = " ".join(f"<span style='background:{_badge_bg(lvl, t)};color:{_badge_fg(lvl, t)};'>"
                         f"&nbsp;{html.escape(txt)}&nbsp;</span>" for txt, lvl in entry.badges)
        out.insert(0, f"<p>{pills}</p>")
    layers = entry.facts.get("layers") or []
    fields = entry.facts.get("fields") or (layers[0].get("fields") if len(layers) == 1 else [])
    if fields:
        title = html.escape(tr("Fields"))
        out.append(f"<h4 style='margin:10px 0 4px 0'>{title} ({len(fields)})</h4>")
        out.append("<table>" + "".join(
            f"<tr><td style='padding:1px 14px 1px 0'>{html.escape(n)}</td>"
            f"<td style='color:{t['text_dim']}'>{html.escape(ty or '')}</td></tr>" for n, ty in fields[:200]) +
                   "</table>")
    if used is not None:
        title = html.escape(tr("Used in this project"))
        out.append(f"<h4 style='margin:10px 0 4px 0'>{title}</h4>")
        out.append(used_html(used, t))
    return "".join(out)


def used_html(used, t):
    if not used:
        text = html.escape(tr("Not used in this project."))
        return f"<p style='color:{t['text_dim']}'>{text}</p>"
    names = {"layer": tr("Layer"), "layout": tr("Layout"), "theme": tr("Map theme"), "stop": tr("Map Stop")}
    return "<table>" + "".join(
        f"<tr><td style='color:{t['text_dim']};padding:2px 14px 2px 0'>{html.escape(names.get(k, k))}</td>"
        f"<td>{html.escape(n)}</td></tr>" for k, n in used) + "</table>"


def _badge_bg(level, t):
    level = "good" if level == "sealed" else level
    dark = QColor(t["base"]).lightness() < 128
    return {"bad": "#5A1D1D" if dark else "#FDE2E1", "warn": "#4A3410" if dark else "#FEF3C7",
            "good": "#123D2A" if dark else "#DCFCE7"}.get(level, t["accent_soft"])


def _badge_fg(level, t):
    level = "good" if level == "sealed" else level
    dark = QColor(t["base"]).lightness() < 128
    return {"bad": "#FF9B9B" if dark else "#B42318", "warn": "#FCD34D" if dark else "#92400E",
            "good": "#6EE7B7" if dark else "#166534"}.get(level, t["accent"])


class _Header(QWidget):
    def __init__(self, entry, t, pixmap=None):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        pic = QLabel()
        pic.setFixedSize(56, 56)
        if pixmap is not None:
            pic.setPixmap(pixmap.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))
        else:
            dark = QColor(t["base"]).lightness() < 128
            pic.setStyleSheet(f"background: {tint(entry.kind, dark)}; border-radius: 12px;")
        h.addWidget(pic)
        v = QVBoxLayout()
        v.setSpacing(2)
        title = QLabel(entry.title)
        title.setObjectName("VaultTitle")
        title.setWordWrap(True)
        sub = QLabel(entry.subtitle or kind_name(entry.kind))
        sub.setObjectName("VaultSub")
        sub.setWordWrap(True)
        v.addWidget(title)
        v.addWidget(sub)
        h.addLayout(v, 1)


class QuickLook(QDialog):
    """A small map of one file - the file is opened for this window only, the project is not touched."""

    def __init__(self, entry, tokens, parent=None, on_add=None, pixmap=None):
        super().__init__(parent)
        from qgis.gui import QgsMapCanvas

        self.entry = entry
        self.setObjectName("VaultDialog")
        self.setWindowTitle(tr("Quick look - {name}").format(name=entry.title))
        self.setStyleSheet(dialog_qss(tokens))
        self.layer = make_layer(entry)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)
        v.addWidget(_Header(entry, tokens, pixmap))
        self.canvas = QgsMapCanvas(self)
        self.canvas.setCanvasColor(QColor(tokens["base"]))
        self.canvas.setMinimumSize(420, 300)
        self.canvas.enableAntiAliasing(True)
        if self.layer is not None:
            if self.layer.crs().isValid():
                self.canvas.setDestinationCrs(self.layer.crs())
            self.canvas.setLayers([self.layer])
            ext = QgsRectangle(self.layer.extent())
            if not ext.isEmpty():
                ext.scale(1.08)
            elif not ext.isNull():
                ext.grow(10)
            self.canvas.setExtent(ext)
            self.canvas.refresh()
        else:
            self.canvas.hide()
            msg = QLabel(tr("This file has nothing to draw on a map."))
            msg.setObjectName("VaultSub")
            msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            msg.setMinimumHeight(120)
            v.addWidget(msg)
        v.addWidget(self.canvas, 1)
        info = QTextBrowser()
        info.setHtml(facts_html(entry, tokens))
        info.setMaximumHeight(150)
        info.setOpenLinks(False)
        v.addWidget(info)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.close)
        row.addWidget(close)
        if on_add is not None and self.layer is not None:
            add = QPushButton(tr("Add to map"))
            add.setObjectName("VaultPrimary")
            add.clicked.connect(lambda: (on_add(entry), self.close()))
            row.addWidget(add)
        v.addLayout(row)
        self.resize(560, 600)

    def in_project(self):
        return self.layer is not None and QgsProject.instance().mapLayer(self.layer.id()) is not None

    def done(self, r):
        self.canvas.setLayers([])
        self.layer = None
        super().done(r)

    def closeEvent(self, e):  # noqa: N802
        """Lets go of the file at once (so it can be renamed or deleted)."""
        self.canvas.setLayers([])
        self.layer = None
        super().closeEvent(e)


class PropertiesCard(QDialog):
    def __init__(self, entry, tokens, parent=None, used=None, pixmap=None, on_open_folder=None):
        super().__init__(parent)
        self.setObjectName("VaultDialog")
        self.setWindowTitle(tr("Properties - {name}").format(name=entry.title))
        self.setStyleSheet(dialog_qss(tokens))
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)
        v.addWidget(_Header(entry, tokens, pixmap))
        self.info = QTextBrowser()
        self.info.setHtml(facts_html(entry, tokens, used))
        v.addWidget(self.info, 1)
        row = QHBoxLayout()
        if entry.path:
            copy = QPushButton(tr("Copy path"))
            copy.clicked.connect(lambda: QApplication.clipboard().setText(entry.path))
            row.addWidget(copy)
            if on_open_folder is not None:
                fold = QPushButton(tr("Open its folder"))
                fold.clicked.connect(lambda: on_open_folder(entry))
                row.addWidget(fold)
        row.addStretch(1)
        close = QPushButton(tr("Close"))
        close.setObjectName("VaultPrimary")
        close.clicked.connect(self.close)
        row.addWidget(close)
        v.addLayout(row)
        self.resize(480, 520)


class ListDialog(QDialog):
    """A titled list (where used / duplicates)."""

    def __init__(self, title, intro, body_html, tokens, parent=None):
        super().__init__(parent)
        self.setObjectName("VaultDialog")
        self.setWindowTitle(title)
        self.setStyleSheet(dialog_qss(tokens))
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        head = QLabel(intro)
        head.setObjectName("VaultTitle")
        head.setWordWrap(True)
        v.addWidget(head)
        self.info = QTextBrowser()
        self.info.setHtml(body_html)
        v.addWidget(self.info, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton(tr("Close"))
        close.setObjectName("VaultPrimary")
        close.clicked.connect(self.close)
        row.addWidget(close)
        v.addLayout(row)
        self.resize(460, 360)


def paths_html(paths, t):
    if not paths:
        text = html.escape(tr("No copies found."))
        return f"<p style='color:{t['text_dim']}'>{text}</p>"
    return "".join(f"<p style='margin:3px 0'><b>{html.escape(os.path.basename(p))}</b><br>"
                   f"<span style='color:{t['text_dim']}'>{html.escape(os.path.dirname(p))}</span></p>" for p in paths)
