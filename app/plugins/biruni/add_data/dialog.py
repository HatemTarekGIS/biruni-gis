"""The Add Data window: one big drop zone, every file recognised by itself (points, lines, areas, tables, images,
elevation, CAD, GPS …) and shown as a card with its quick facts, then one Add button. Side tabs for web maps and for
databases & services (which open the classic window on the right page)."""
import os
import sys

from qgis.core import QgsProject, QgsSettings
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QKeySequence, QShortcut
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                                 QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar, QPushButton,
                                 QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QToolButton, QVBoxLayout, QWidget)

from .. import icons
from ..i18n import format_number, tr
from . import detect
from .kinds import icon_name, kind_name, tint

LAST_DIR_KEY = "biruni/add_data/last_dir"
KEEP_OPEN_KEY = "biruni/add_data/keep_open"
RECENT_SHOWN = 8

# the stock Data Source Manager pages offered on "Databases & services": (page, label, icon, is a product name)
SERVICE_PAGES = (
    ("postgres", "PostgreSQL / PostGIS", "database"),
    ("spatialite", "SpatiaLite", "database"),
    ("mssql", "MS SQL Server", "database"),
    ("hana", "SAP HANA", "database"),
    ("wms", "WMS / WMTS", "world-download"),
    ("WFS", "WFS / OGC API", "world-share"),
    ("wcs", "WCS", "world"),
    ("xyz", "XYZ", "grid-3x3"),
    ("arcgisfeatureserver", "ArcGIS REST", "world-cog"),
    ("vectortile", "Vector tiles", "stack-2"),
    ("tiledscene", "3D", "building"),
    ("stac", "STAC", "map-search"),
)


def _rgba(hex_colour, alpha):
    h = hex_colour.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


def _service_text(page):
    if page in ("postgres", "spatialite", "mssql", "hana"):
        return tr("Tables from a database")
    if page == "wms":
        return tr("Maps from a web map server")
    if page == "WFS":
        return tr("Features from a web server")
    if page == "wcs":
        return tr("Rasters from a web coverage server")
    if page == "xyz":
        return tr("Tiled web maps")
    if page == "arcgisfeatureserver":
        return tr("Map and feature services")
    if page == "vectortile":
        return tr("Vector tile services and files")
    if page == "tiledscene":
        return tr("3D tiles and scenes")
    if page == "stac":
        return tr("Satellite image catalogues")
    return ""


def qss(t, dark):
    soft = t.get("accent_soft", t["hover"])
    return f"""
#BiruniAddData {{ background: {t['window']}; }}
#BiruniAddData QLabel {{ color: {t['text']}; }}
#BiruniAddData QLabel[dim="true"] {{ color: {t['text_dim']}; }}
#BiruniAddData QLabel#AddDataTitle {{ font-size: 13pt; font-weight: 700; }}
#BiruniAddData QLabel#AddDataSection {{ font-size: 11pt; font-weight: 600; }}
#BiruniAddData QListWidget#AddDataNav {{ background: {t['panel']}; border: none; padding: 8px 6px; outline: 0; }}
#BiruniAddData QListWidget#AddDataNav::item {{ padding: 9px 10px; border-radius: 8px; color: {t['text']};
    margin: 1px 0; }}
#BiruniAddData QListWidget#AddDataNav::item:hover {{ background: {t['hover']}; }}
#BiruniAddData QListWidget#AddDataNav::item:selected {{ background: {soft}; color: {t['text']}; font-weight: 600; }}
#BiruniAddData QFrame#AddDataDrop {{ background: {t['card']}; border: 2px dashed {t['mid'] if dark else t['dark']};
    border-radius: 14px; }}
#BiruniAddData QFrame#AddDataDrop[hot="true"] {{ background: {soft}; border-color: {t['accent']}; }}
#BiruniAddData QLabel#AddDataDropTitle {{ font-size: 12pt; font-weight: 600; }}
#BiruniAddData QPushButton {{ border: 1px solid {t['border']}; border-radius: 7px; padding: 6px 14px;
    background: {t['button']}; color: {t['text']}; }}
#BiruniAddData QPushButton:hover {{ background: {t['hover']}; }}
#BiruniAddData QPushButton:disabled {{ color: {t['text_disabled']}; }}
#BiruniAddData QPushButton#AddDataPrimary {{ background: {t['accent']}; color: {t['accent_text']}; border: none;
    font-weight: 600; padding: 7px 18px; }}
#BiruniAddData QPushButton#AddDataPrimary:hover {{ background: {t['accent_hover']}; }}
#BiruniAddData QPushButton#AddDataPrimary:disabled {{ background: {t['border']}; color: {t['text_disabled']}; }}
#BiruniAddData QPushButton#AddDataTile {{ padding: 10px 12px; border-radius: 10px;
    background: {t['card']}; }}
#BiruniAddData QPushButton#AddDataTile:hover {{ background: {t.get('card_hover', t['hover'])};
    border-color: {t['accent']}; }}
#BiruniAddData QToolButton {{ border: none; border-radius: 6px; padding: 3px 6px; color: {t['accent']};
    background: transparent; }}
#BiruniAddData QToolButton:hover {{ background: {t['hover']}; }}
#BiruniAddData QToolButton#AddDataChip {{ border: 1px solid {t['border']}; border-radius: 12px; padding: 3px 10px;
    color: {t['text']}; background: {t['card']}; }}
#BiruniAddData QToolButton#AddDataChip:hover {{ border-color: {t['accent']}; }}
#BiruniAddData QScrollArea#AddDataList {{ border: none; background: transparent; }}
#BiruniAddData QWidget#AddDataListBody {{ background: transparent; }}
#BiruniAddData QFrame#AddDataCard {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 10px; }}
#BiruniAddData QFrame#AddDataCard:hover {{ border-color: {t['accent']}; }}
#BiruniAddData QFrame#AddDataCard[off="true"] {{ background: {t['alt_base']}; }}
#BiruniAddData QFrame#AddDataCard[off="true"] QLabel {{ color: {t['text_disabled']}; }}
#BiruniAddData QLabel#AddDataCardTitle {{ font-weight: 600; }}
#BiruniAddData QLabel#AddDataCardSub {{ color: {t['text_dim']}; font-size: 8.5pt; }}
#BiruniAddData QLineEdit {{ border: 1px solid {t['border']}; border-radius: 7px; padding: 6px 8px;
    background: {t['base']}; color: {t['text']}; }}
#BiruniAddData QProgressBar {{ border: none; background: {t['border']}; border-radius: 2px; max-height: 4px; }}
#BiruniAddData QProgressBar::chunk {{ background: {t['accent']}; border-radius: 2px; }}
#BiruniAddData QTreeWidget#AddDataTree, #BiruniAddData QListWidget#AddDataPlaces {{ background: {t['card']};
    border: 1px solid {t['border']}; border-radius: 8px; color: {t['text']}; outline: 0; }}
#BiruniAddData QTreeWidget#AddDataTree::item {{ padding: 3px 2px; }}
#BiruniAddData QTreeWidget#AddDataTree::item:hover, #BiruniAddData QListWidget#AddDataPlaces::item:hover {{
    background: {t['hover']}; }}
#BiruniAddData QTreeWidget#AddDataTree::item:selected, #BiruniAddData QListWidget#AddDataPlaces::item:selected {{
    background: {soft}; color: {t['text']}; }}
#BiruniAddData QListWidget#AddDataPlaces::item {{ padding: 4px 6px; border-radius: 6px; }}
#BiruniAddData QHeaderView::section {{ background: {t['alt_base']}; color: {t['text_dim']}; border: none;
    border-bottom: 1px solid {t['border']}; padding: 4px 6px; }}
#BiruniAddData QToolButton#AddDataCrumb {{ color: {t['text']}; padding: 3px 5px; }}
#BiruniAddData QFrame#AddDataFooter {{ background: {t['panel']}; border-top: 1px solid {t['border']}; }}
"""


# ---- one card per layer -------------------------------------------------------------------------------------------
class Card(QFrame):
    def __init__(self, item, dlg):
        super().__init__()
        self.item = item
        self.dlg = dlg
        self.setObjectName("AddDataCard")
        t, dark = dlg.tokens(), dlg.dark()
        off = not item["readable"] and item["kind"] not in ("project", "service")
        self.setProperty("off", "true" if off else "false")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 8, 8)
        lay.setSpacing(10)
        self.check = QCheckBox()
        self.check.setChecked(bool(item["checked"]) and item["readable"])
        self.check.setEnabled(item["readable"] and item["layer_type"] not in ("project", "service"))
        self.check.setVisible(item["layer_type"] not in ("project", "service"))
        self.check.toggled.connect(self._toggled)
        self.check.setAccessibleName(item["name"])
        lay.addWidget(self.check)
        colour = tint(item["kind"], dark)
        badge = QLabel()
        badge.setFixedSize(38, 38)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setPixmap(icons.icon(icon_name(item["kind"]), colour).pixmap(22, 22))
        badge.setStyleSheet(f"background: {_rgba(colour, 0.16)}; border-radius: 9px;")
        lay.addWidget(badge)
        text = QVBoxLayout()
        text.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = QLabel(item["name"])
        self.title.setObjectName("AddDataCardTitle")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(self.title, 1)
        self.chip = QLabel(kind_name(item["kind"]))
        self.chip.setStyleSheet(f"color: {colour}; background: {_rgba(colour, 0.12)}; border-radius: 8px;"
                                f" padding: 1px 8px; font-size: 8.5pt; font-weight: 600;")
        top.addWidget(self.chip, 0)
        text.addLayout(top)
        self.sub = QLabel(" · ".join(facts(item, dlg.project_crs_name)))
        self.sub.setObjectName("AddDataCardSub")
        self.sub.setTextFormat(Qt.TextFormat.PlainText)
        self.sub.setWordWrap(True)
        text.addWidget(self.sub)
        lay.addLayout(text, 1)
        self.action = None
        if item["layer_type"] == "project":
            self.action = QPushButton(tr("Open project"))
            self.action.clicked.connect(lambda: dlg.open_project(item["path"]))
        elif item["layer_type"] == "service":
            self.action = QPushButton(tr("Choose layers…"))
            self.action.clicked.connect(lambda: dlg.open_service(item))
        if self.action is not None:
            self.action.setCursor(Qt.CursorShape.PointingHandCursor)
            lay.addWidget(self.action)
        self.remove = QToolButton()
        self.remove.setIcon(icons.icon("x", t["text_dim"]))
        self.remove.setToolTip(tr("Remove from the list"))
        self.remove.clicked.connect(lambda: dlg.remove_card(self))
        lay.addWidget(self.remove, 0, Qt.AlignmentFlag.AlignTop)
        self.setToolTip(item["path"])

    def _toggled(self, on):
        self.item["checked"] = bool(on)
        self.dlg.update_counts()


def facts(item, project_crs_name=""):
    """The quick facts of an item, translated, in reading order."""
    out = []
    path = item["path"]
    base = os.path.basename(path.rstrip("/\\")) if not detect.is_url(path) else ""
    if item.get("container") == "archive":
        out.append(tr("inside {name}").format(name=base))
    elif base and base != item["name"]:
        out.append(base)
    if item["kind"] == "unknown" or item["kind"] == "archive":
        note = item.get("note")
        if note == "missing":
            out.append(tr("This file does not exist (any more)."))
        elif note == "folder":
            out.append(tr("No map data found in this folder."))
        elif note == "empty archive":
            out.append(tr("Nothing that can be read was found inside."))
        else:
            out.append(tr("This is not map data that can be read - it will not be added."))
        return out
    if item["kind"] == "service":
        if item.get("note") == "unknown address":
            out.append(tr("An address that is not recognised; try it as a web map service."))
        else:
            out.append(tr("Choose which of its layers to add."))
        return out
    if item["kind"] == "project":
        out.append(tr("A whole map project - opening it replaces the current map."))
        if item.get("size"):
            out.append(detect.human_size(item["size"]))
        return out
    if item.get("inside", 1) > 1 and item.get("container") != "archive":
        out.append(tr("1 of {n} layers in the file").format(n=format_number(item["inside"], 0)))
    sub_kind = item.get("cad_kind") or item.get("gps_kind")
    if sub_kind and sub_kind != item["kind"]:
        out.append(kind_name(sub_kind))
    if item.get("count") is not None:
        n = item["count"]
        if item["kind"] in ("table", "spreadsheet"):
            out.append(tr("1 row") if n == 1 else tr("{n} rows").format(n=format_number(n, 0)))
        elif item["kind"] == "pointcloud":
            out.append(tr("{n} points").format(n=format_number(n, 0)))
        else:
            out.append(tr("1 feature") if n == 1 else tr("{n} features").format(n=format_number(n, 0)))
    if item.get("pixels"):
        w, h = item["pixels"]
        out.append(tr("{w} × {h} pixels").format(w=format_number(w, 0), h=format_number(h, 0)))
    if item.get("bands", 0) > 1:
        out.append(tr("{n} bands").format(n=item["bands"]))
    if item.get("xy"):
        out.append(tr("points from the columns {x} and {y}").format(x=item["xy"][0], y=item["xy"][1]))
    elif item.get("wkt"):
        out.append(tr("shapes from the column {c}").format(c=item["wkt"]))
    if item.get("crs_guessed"):
        out.append(tr("{crs} (guessed: the values are degrees)").format(crs=_crs_label(item["crs"])))
    elif item.get("crs_from_project"):
        if item.get("crs"):
            out.append(tr("uses the map's coordinate system {crs} - please check it").format(
                crs=project_crs_name or item["crs"]))
        else:
            out.append(tr("no coordinate system - it takes the map's"))
    elif item.get("crs"):
        out.append(item["crs"])
    elif item["layer_type"] in ("vector", "raster") and item["kind"] not in ("table", "spreadsheet", "web"):
        out.append(tr("no coordinate system"))
    if item.get("size"):
        out.append(detect.human_size(item["size"]))
    return out


def _crs_label(authid):
    if authid == "EPSG:4326":
        return "WGS 84 (EPSG:4326)"
    return authid


# ---- the window ---------------------------------------------------------------------------------------------------
class AddDataDialog(QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.iface = plugin.iface
        self.setObjectName("BiruniAddData")
        self.setWindowTitle(tr("Add data"))
        self.setAcceptDrops(True)
        self.resize(1100, 680)
        self.cards = []
        self.keys = set()          # what is in the list or being read (no doubles)
        self.total = 0             # things being read in this round
        self.finished_jobs = 0
        self.truncated = False
        self.jobs = {}             # job id -> source
        self._n = 0
        self.auto_add = set()      # sources added straight to the map when read (double-click in the browser)
        self.add_when_ready = None  # Add was pressed while things were still being read: keep_open to use
        self.browser = None
        prj = QgsProject.instance()
        self.project_crs = prj.crs().authid() if prj.crs().isValid() else ""
        self.project_crs_name = prj.crs().userFriendlyIdentifier() if prj.crs().isValid() else ""
        from ..vault.worker import Worker

        self.worker = Worker(self)
        self.worker.done.connect(self._job_done)
        self.finished.connect(lambda *_: self._stop())
        self._build()
        self.setStyleSheet(qss(self.tokens(), self.dark()))
        self.update_counts()

    def _stop(self):
        self.worker.stop()
        if self.browser is not None:
            self.browser.worker.stop()

    # -- theme --------------------------------------------------------------------------------------------------
    def tokens(self):
        theme = getattr(self.plugin, "theme", None)
        if theme is not None:
            return theme.tokens()
        from ..theme import TOKENS

        return TOKENS["light"]

    def dark(self):
        theme = getattr(self.plugin, "theme", None)
        return bool(theme is not None and theme.is_dark())

    # -- building -----------------------------------------------------------------------------------------------
    def _build(self):
        t = self.tokens()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        root.addLayout(body, 1)
        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("AddDataNav")
        self.nav.setIconSize(QSize(20, 20))
        self.nav.setFixedWidth(170)
        for label, ic in ((tr("Files"), "files"), (tr("Web maps"), "world"),
                          (tr("Databases & services"), "database")):
            QListWidgetItem(icons.icon(ic, t["icon"]), label, self.nav)
        side.addWidget(self.nav, 1)
        self.classic = QLabel(f"<a href='#' style='color:{t['accent']}; text-decoration:none'>"
                              f"{tr('Classic Data Source Manager…')}</a>")
        self.classic.setProperty("dim", "true")
        self.classic.setContentsMargins(14, 8, 8, 12)
        self.classic.setWordWrap(True)
        self.classic.linkActivated.connect(lambda *_: self.open_classic())
        side.addWidget(self.classic)
        sidew = QWidget()
        sidew.setLayout(side)
        body.addWidget(sidew)
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        self.stack.addWidget(self._files_page())
        self.stack.addWidget(self._web_page())
        self.stack.addWidget(self._services_page())
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.currentRowChanged.connect(lambda i: self.footer_add.setVisible(i == 0))
        # footer
        footer = QFrame()
        footer.setObjectName("AddDataFooter")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(16, 10, 16, 10)
        self.keep_open = QCheckBox(tr("Keep this window open after adding"))
        self.keep_open.setChecked(QgsSettings().value(KEEP_OPEN_KEY, False, type=bool))
        self.keep_open.toggled.connect(lambda on: QgsSettings().setValue(KEEP_OPEN_KEY, bool(on)))
        fl.addWidget(self.keep_open)
        fl.addStretch(1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.reject)
        fl.addWidget(close)
        self.footer_add = QWidget()
        al = QHBoxLayout(self.footer_add)
        al.setContentsMargins(0, 0, 0, 0)
        self.add_button = QPushButton(tr("Add"))
        self.add_button.setObjectName("AddDataPrimary")
        self.add_button.setDefault(True)
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.clicked.connect(lambda: self.add_checked())
        al.addWidget(self.add_button)
        fl.addWidget(self.footer_add)
        root.addWidget(footer)
        self.nav.setCurrentRow(0)

    def _page(self, title, text=""):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 18, 20, 12)
        lay.setSpacing(10)
        h = QLabel(title)
        h.setObjectName("AddDataTitle")
        lay.addWidget(h)
        if text:
            s = QLabel(text)
            s.setProperty("dim", "true")
            s.setWordWrap(True)
            lay.addWidget(s)
        return w, lay

    def _files_page(self):
        t = self.tokens()
        w, page = self._page(tr("Add data"))
        # the browser on one side, the Ready to add list on the other
        from .browser import Browser

        self.browser = Browser(self)
        right = QWidget()
        lay = QVBoxLayout(right)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.addWidget(self.browser)
        self.split.addWidget(right)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.split.setSizes([560, 360])
        page.addWidget(self.split, 1)
        right.setMinimumWidth(280)
        # the drop zone
        self.drop = QFrame()
        self.drop.setObjectName("AddDataDrop")
        self.drop.setProperty("hot", "false")
        dl = QVBoxLayout(self.drop)
        dl.setContentsMargins(12, 10, 12, 10)
        dl.setSpacing(6)
        pic = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        pic.setPixmap(icons.icon("file-import", t["accent"]).pixmap(32, 32))
        self.drop_pic = pic
        dl.addWidget(pic)
        title = QLabel(tr("Drop files or folders here"), alignment=Qt.AlignmentFlag.AlignCenter)
        title.setObjectName("AddDataDropTitle")
        title.setWordWrap(True)
        dl.addWidget(title)
        self.drop_text = QLabel(tr("Shapefiles, GeoPackages, CAD drawings, GPS tracks, spreadsheets, images, point "
                                   "clouds… Each one is recognised by itself."),
                                alignment=Qt.AlignmentFlag.AlignCenter)
        self.drop_text.setProperty("dim", "true")
        self.drop_text.setWordWrap(True)
        dl.addWidget(self.drop_text)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.files_button = QPushButton(icons.icon("files", t["icon"]), tr("Choose files…"))
        self.files_button.clicked.connect(self.choose_files)
        buttons.addWidget(self.files_button)
        self.folder_button = QPushButton(icons.icon("folder-open", t["icon"]), tr("Choose a folder…"))
        self.folder_button.clicked.connect(self.choose_folder)
        buttons.addWidget(self.folder_button)
        buttons.addStretch(1)
        dl.addLayout(buttons)
        hint = QLabel(tr("You can also paste a file path or a web address (Ctrl+V)."),
                      alignment=Qt.AlignmentFlag.AlignCenter)
        hint.setProperty("dim", "true")
        hint.setWordWrap(True)
        dl.addWidget(hint)
        lay.addWidget(self.drop)
        # recent
        self.recent_row = QWidget()
        rl = QHBoxLayout(self.recent_row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        rlab = QLabel(tr("Recent:"))
        rlab.setProperty("dim", "true")
        rl.addWidget(rlab)
        self.recent_buttons = []
        for path in recent_files():
            b = QToolButton()
            b.setObjectName("AddDataChip")
            name = detect.stem_of(path)
            b.setText(name if len(name) <= 22 else name[:20] + "…")
            b.setToolTip(path)
            b.setIcon(icons.icon("history", t["icon"]))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, p=path: self.add_paths([p]))
            rl.addWidget(b)
            self.recent_buttons.append(b)
        rl.addStretch(1)
        self.recent_row.setObjectName("AddDataListBody")
        self.recent_scroll = QScrollArea()  # a long list of recent files scrolls sideways (never widens the window)
        self.recent_scroll.setObjectName("AddDataList")
        self.recent_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.recent_scroll.setWidgetResizable(True)
        self.recent_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent_scroll.setWidget(self.recent_row)
        self.recent_scroll.setFixedHeight(self.recent_row.sizeHint().height() + 14)
        self.recent_scroll.setVisible(bool(self.recent_buttons))
        lay.addWidget(self.recent_scroll)
        ready = QLabel(tr("Ready to add"))
        ready.setObjectName("AddDataSection")
        lay.addWidget(ready)
        # status / list tools
        status = QHBoxLayout()
        self.status = QLabel()
        self.status.setProperty("dim", "true")
        self.status.setWordWrap(True)
        status.addWidget(self.status, 1)
        self.select_all = QToolButton(text=tr("Select all"))
        self.select_all.clicked.connect(lambda: self.set_all(True))
        self.select_none = QToolButton(text=tr("Select none"))
        self.select_none.clicked.connect(lambda: self.set_all(False))
        self.clear_button = QToolButton(text=tr("Clear the list"))
        self.clear_button.clicked.connect(self.clear)
        for b in (self.select_all, self.select_none, self.clear_button):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            status.addWidget(b)
        lay.addLayout(status)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        lay.addWidget(self.progress)
        # the cards
        self.scroll = QScrollArea()
        self.scroll.setObjectName("AddDataList")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        bodyw = QWidget()
        bodyw.setObjectName("AddDataListBody")
        self.list_layout = QVBoxLayout(bodyw)
        self.list_layout.setContentsMargins(0, 0, 4, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(bodyw)
        lay.addWidget(self.scroll, 1)
        self.paste_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Paste), w)
        self.paste_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.paste_shortcut.activated.connect(self.paste)
        return w

    def _web_page(self):
        t = self.tokens()
        w, lay = self._page(tr("Web maps"), tr("Ready-made background maps from the internet. They are added under "
                                               "your layers."))
        grid = QGridLayout()
        grid.setSpacing(8)
        self.basemap_buttons = {}
        for i, (key, name) in enumerate(basemaps(self.plugin)):
            b = QPushButton(icons.icon("map-2", t["accent"]), name)
            b.setObjectName("AddDataTile")
            b.setIconSize(QSize(22, 22))
            b.setMinimumHeight(48)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key, n=name: self.add_basemap(k, n))
            grid.addWidget(b, i // 2, i % 2)
            self.basemap_buttons[key] = b
        lay.addLayout(grid)
        other = QPushButton(icons.icon("grid-3x3", t["icon"]), tr("Another tiled web map (XYZ / WMTS)…"))
        other.setObjectName("AddDataTile")
        other.clicked.connect(lambda: self.open_page("xyz"))
        lay.addWidget(other)
        sec = QLabel(tr("Paste a web address"))
        sec.setObjectName("AddDataSection")
        lay.addSpacing(8)
        lay.addWidget(sec)
        s = QLabel(tr("A tile address with {z}/{x}/{y}, an ArcGIS REST service, a WMS / WFS server or a link to a "
                      "GeoJSON, KML or ZIP file - it is recognised by itself."))
        s.setProperty("dim", "true")
        s.setWordWrap(True)
        lay.addWidget(s)
        row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://…")
        self.url_edit.returnPressed.connect(self.add_url)
        row.addWidget(self.url_edit, 1)
        go = QPushButton(tr("Recognise"))
        go.clicked.connect(self.add_url)
        row.addWidget(go)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def _services_page(self):
        t = self.tokens()
        w, lay = self._page(tr("Databases & services"),
                            tr("Connect to a database or a web server. The classic window opens on the right page."))
        body = QWidget()
        body.setObjectName("AddDataListBody")
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 4, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        self.service_buttons = {}
        for i, (page, label, ic) in enumerate(SERVICE_PAGES):
            b = QPushButton(icons.icon(ic, t["accent"]), label)
            b.setObjectName("AddDataTile")
            b.setIconSize(QSize(20, 20))
            b.setMinimumWidth(190)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, p=page: self.open_page(p))
            grid.addWidget(b, i, 0)
            d = QLabel(_service_text(page))
            d.setProperty("dim", "true")
            d.setWordWrap(True)
            grid.addWidget(d, i, 1)
            self.service_buttons[page] = b
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(len(SERVICE_PAGES), 1)
        scroll = QScrollArea()
        scroll.setObjectName("AddDataList")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        return w

    # -- drag and drop, paste -----------------------------------------------------------------------------------
    def _hot(self, on):
        self.drop.setProperty("hot", "true" if on else "false")
        self.drop.style().unpolish(self.drop)
        self.drop.style().polish(self.drop)

    def dragEnterEvent(self, e):  # noqa: N802
        from qgis.core import QgsMimeDataUtils

        md = e.mimeData()
        if md.hasUrls() or md.hasText() or QgsMimeDataUtils.isUriList(md):
            e.acceptProposedAction()
            self._hot(True)

    def dragMoveEvent(self, e):  # noqa: N802
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):  # noqa: N802
        self._hot(False)

    def dropEvent(self, e):  # noqa: N802
        self._hot(False)
        if self.handle_mime(e.mimeData()):
            e.acceptProposedAction()

    def handle_mime(self, md):
        """Dropped data: layers dragged from the browser (or the engine's browser), files and web addresses.
        Returns how many things were taken."""
        from qgis.core import QgsMimeDataUtils

        n = 0
        if QgsMimeDataUtils.isUriList(md):
            for u in QgsMimeDataUtils.decodeUriList(md):
                it = self.browser.known.get((u.providerKey, u.uri)) if self.browser is not None else None
                if it is None:
                    lt = {"vector-tile": "vectortile", "tiled-scene": "tiledscene"}.get(u.layerType, u.layerType)
                    it = detect._item(u.uri, u.uri, name=u.name, provider=u.providerKey, uri=u.uri, layer_type=lt,
                                      readable=True, checked=True,
                                      kind="shapes" if lt == "vector" else "image" if lt == "raster" else lt)
                self.add_item_card(it)
                n += 1
        sources = sources_from_mime(md)
        if sources:
            self.add_paths(sources)
            n += len(sources)
        if n:
            self.nav.setCurrentRow(0)
        return n

    def paste(self):
        sources = sources_from_mime(QApplication.clipboard().mimeData())
        if not sources:
            self.status.setText(tr("The clipboard holds no file path or web address."))
            return 0
        self.add_paths(sources)
        return len(sources)

    def add_url(self):
        text = self.url_edit.text().strip()
        if not text:
            return
        self.url_edit.clear()
        self.nav.setCurrentRow(0)
        self.add_paths([text])

    # -- choosing -----------------------------------------------------------------------------------------------
    def _last_dir(self):
        d = QgsSettings().value(LAST_DIR_KEY, "") or ""
        if not d:
            home = QgsProject.instance().homePath()
            d = home or os.path.expanduser("~")
        return d

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Choose files to add"), self._last_dir(),
                                                tr("All files") + " (*)")
        if paths:
            QgsSettings().setValue(LAST_DIR_KEY, os.path.dirname(paths[0]))
            self.add_paths(paths)

    def choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, tr("Choose a folder"), self._last_dir())
        if d:
            QgsSettings().setValue(LAST_DIR_KEY, d)
            self.add_paths([d])

    # -- reading (in the background thread) ---------------------------------------------------------------------
    def _key(self, source):
        if source.startswith("layer:") or detect.is_url(source):
            return source.strip()
        return os.path.normcase(os.path.abspath(source))

    def add_paths(self, sources, auto_add=False):
        """Starts reading files / folders / addresses; their cards appear as each one is recognised (auto_add: they
        go straight onto the map instead). Anything inside a File Geodatabase means the whole geodatabase."""
        added = 0
        for src in sources:
            src = (src or "").strip().strip('"').strip("'")
            if not src:
                continue
            src = detect.gdb_root(src) or src
            k = self._key(src)
            if k in self.keys:
                continue
            self.keys.add(k)
            if auto_add:
                self.auto_add.add(k)
            is_folder = (not detect.is_url(src) and os.path.isdir(src)
                         and not src.lower().rstrip("/\\").endswith(".gdb"))
            self._submit("folder" if is_folder else "detect", src)
            added += 1
        if not added and sources:
            self.status.setText(tr("Already in the list."))
        return added

    def _submit(self, what, src):
        self._n += 1
        job = f"{what}:{self._n}"
        self.jobs[job] = src
        self.total += 1
        if what == "folder":
            self.worker.submit(job, detect.list_folder, src)
        else:
            self.worker.submit(job, detect.detect, src, self.project_crs)
        self.update_counts()

    def _job_done(self, job, result, _gen):
        src = self.jobs.pop(job, None)
        if src is None:
            return
        self.finished_jobs += 1
        if isinstance(result, Exception):
            result = [detect.unknown(src, src)]
        if job.startswith("folder:"):
            paths, truncated = result
            self.truncated = self.truncated or truncated
            fresh = [p for p in paths if self._key(p) not in self.keys]
            for p in fresh:
                self.keys.add(self._key(p))
                self._submit("detect", p)
            if not paths:
                self._add_card(detect.unknown(src, src, note="folder"))
        elif self._key(src) in self.auto_add:  # double-clicked in the browser: straight onto the map
            self.auto_add.discard(self._key(src))
            self.keys.discard(self._key(src))
            ready = [i for i in result if i["readable"] and i["checked"]
                     and i["layer_type"] not in ("project", "service")]
            if ready:
                self.add_items(ready)
            else:
                for item in result:
                    self._add_card(item)
        else:
            for item in result:
                self._add_card(item)
        if not self.jobs:
            self.total = self.finished_jobs = 0
        self.update_counts()
        if self.browser is not None:
            self.browser.sync_ticks()
        if not self.jobs and self.add_when_ready is not None:
            keep, self.add_when_ready = self.add_when_ready, None
            self.add_checked(keep_open=keep)

    def busy(self):
        return bool(self.jobs)

    def _add_card(self, item):
        if item["readable"] and item.get("uri"):
            same = (item["provider"], item["uri"])
            if any((c.item["provider"], c.item["uri"]) == same for c in self.cards):
                return None  # already in the list (ticked in the browser, then its file added)
        card = Card(item, self)
        self.list_layout.insertWidget(self.list_layout.count() - 1, card)
        self.cards.append(card)
        return card

    def add_item_card(self, item):
        """A card for one known layer (from the browser), without reading its file again."""
        it = dict(item)
        it["source"] = f"layer:{it['provider']}:{it['uri']}"
        it["checked"] = True
        card = self._add_card(it)
        if card is not None:
            self.keys.add(self._key(it["source"]))
        self.update_counts()
        if self.browser is not None:
            self.browser.sync_ticks()
        return card

    def has_card_for(self, row):
        """Whether a browser row (file, container or layer) is in the Ready to add list (or being read for it)."""
        it = row.get("item")
        if it is not None and row["type"] in ("layer", "file"):
            same = (it["provider"], it["uri"])
            if any((c.item["provider"], c.item["uri"]) == same for c in self.cards):
                return True
        if row["type"] in ("file", "container"):
            return self._key(row["path"]) in self.keys
        return False

    def remove_cards_for(self, row):
        """Unticked in the browser: its cards leave the list."""
        it = row.get("item")
        drop = []
        for c in self.cards:
            if it is not None and (c.item["provider"], c.item["uri"]) == (it["provider"], it["uri"]):
                drop.append(c)
            elif row["type"] in ("file", "container") and self._key(c.item["source"]) == self._key(row["path"]):
                drop.append(c)
        for c in drop:
            self.remove_card(c)
        self.keys.discard(self._key(row["path"]))
        if self.browser is not None:
            self.browser.sync_ticks()

    # -- the list -----------------------------------------------------------------------------------------------
    def addable(self):
        return [c for c in self.cards if c.item["readable"] and c.item["checked"]
                and c.item["layer_type"] not in ("project", "service")]

    def update_counts(self):
        n = len(self.addable())
        picked = self.browser.selected_rows() if self.browser is not None else []
        if n == 0 and picked:
            self.add_button.setText(tr("Add selected"))
            self.add_button.setEnabled(True)
        elif n == 0:
            self.add_button.setText(tr("Add"))
        elif n == 1:
            self.add_button.setText(tr("Add 1 layer"))
        else:
            self.add_button.setText(tr("Add {n} layers").format(n=format_number(n, 0)))
        self.add_button.setEnabled(n > 0 or bool(picked))
        has = bool(self.cards)
        for b in (self.select_all, self.select_none, self.clear_button):
            b.setVisible(has)
        self.drop_text.setVisible(not has)
        self.drop_pic.setVisible(not has)
        if self.jobs:
            self.progress.show()
            self.progress.setRange(0, max(1, self.total))
            self.progress.setValue(self.finished_jobs)
            self.status.setText(tr("Reading {done} of {total}…").format(done=format_number(self.finished_jobs, 0),
                                                                       total=format_number(self.total, 0)))
        else:
            self.progress.hide()
            if not has:
                self.status.setText(tr("Nothing here yet - each file you add is recognised by itself."))
            else:
                text = tr("{n} of {m} ticked to add.").format(n=format_number(n, 0),
                                                              m=format_number(len(self.cards), 0))
                if self.truncated:
                    text += " " + tr("Only the first {n} files of a folder are listed.").format(
                        n=format_number(detect.FOLDER_MAX, 0))
                self.status.setText(text)

    def set_all(self, on):
        for c in self.cards:
            if c.check.isEnabled():
                c.check.setChecked(on)
        self.update_counts()

    def remove_card(self, card):
        if card in self.cards:
            self.cards.remove(card)
        if not any(self._key(c.item["source"]) == self._key(card.item["source"]) for c in self.cards):
            self.keys.discard(self._key(card.item["source"]))
        card.hide()
        card.deleteLater()
        self.update_counts()
        if self.browser is not None:
            self.browser.sync_ticks()

    def clear(self):
        for c in list(self.cards):
            c.hide()
            c.deleteLater()
        self.cards = []
        self.keys = {self._key(s) for s in self.jobs.values()}
        self.truncated = False
        self.update_counts()
        if self.browser is not None:
            self.browser.sync_ticks()

    # -- adding -------------------------------------------------------------------------------------------------
    def add_items(self, items):
        """Puts layers on the map in one batch (images under shapes, points on top) and says so. Returns the
        layers and the names that did not open."""
        items = sorted(items, key=lambda i: detect.DRAW_ORDER.get(i["kind"], 6))
        layers, failed = [], []
        for it in items:
            lyr = make_layer(it)
            if lyr is None:
                failed.append(it["name"])
            else:
                layers.append(lyr)
        if layers:
            # one batch: the first of the list ends up on top of the layer tree
            QgsProject.instance().addMapLayers(list(reversed(layers)))
        self._say(len(layers), failed)
        return layers, failed

    def add_checked(self, keep_open=None):
        """Adds every ticked card - and the rows selected in the browser - to the map. When some of those are still
        being read, they are added as soon as they are ready. Returns the new layers."""
        if self.browser is not None and self.browser.send_selection() and self.busy():
            self.add_when_ready = keep_open
            self.update_counts()
            return []
        cards = self.addable()
        ok_items = []
        for c in cards:
            ok_items.append(c.item)
        layers, failed = self.add_items(ok_items)
        failed_set = set(failed)
        for c in cards:
            if c.item["name"] in failed_set:
                continue
            if c in self.cards:
                self.cards.remove(c)
            c.hide()
            c.deleteLater()
        self.keys = ({self._key(c.item["source"]) for c in self.cards}
                     | {self._key(s) for s in self.jobs.values()})
        self.update_counts()
        if self.browser is not None:
            self.browser.sync_ticks()
        keep = self.keep_open.isChecked() if keep_open is None else keep_open
        if layers and not keep:
            self.accept()
        return layers

    def _say(self, n, failed):
        from ..version import APP_NAME

        bar = self.iface.messageBar()
        if n:
            text = tr("1 layer added") if n == 1 else tr("{n} layers added").format(n=format_number(n, 0))
            bar.pushSuccess(APP_NAME, text)
        if failed:
            bar.pushWarning(APP_NAME, tr("Could not open: {names}").format(names=", ".join(failed[:5])))

    def add_basemap(self, key, name=""):
        lyr = None
        fn = getattr(self.plugin, "add_basemap", None)
        if fn is not None:
            lyr = fn(key)
        if lyr is not None:
            from ..version import APP_NAME

            self.iface.messageBar().pushSuccess(APP_NAME, tr("{name} added").format(name=name or lyr.name()))
            if not self.keep_open.isChecked():
                self.accept()
        return lyr

    # -- other windows ------------------------------------------------------------------------------------------
    def open_project(self, path):
        self.accept()
        self.iface.addProject(path)

    def open_service(self, item):
        if detect.is_url(item["path"]):
            QApplication.clipboard().setText(item["path"])
            from ..version import APP_NAME

            self.iface.messageBar().pushInfo(APP_NAME, tr("The address is copied - paste it as a new connection."))
        self.open_page(item.get("page") or "wms")

    def open_page(self, page):
        """The classic window on one of its pages (the whole classic window when that is not possible)."""
        fn = getattr(self.iface, "openDataSourceManagerPage", None)
        if not self.keep_open.isChecked():
            self.accept()
        if fn is not None:
            try:
                fn(page)
                return True
            except Exception:  # noqa: BLE001 - an older engine: the whole window
                pass
        return self.open_classic(close=False)

    def open_classic(self, close=True):
        if close and not self.keep_open.isChecked():
            self.accept()
        act = None
        runner = getattr(self.plugin, "runner", None)
        if runner is not None:
            act = runner.qaction("mActionDataSourceManager")
        if act is None:
            from qgis.PyQt.QtGui import QAction

            mw = self.iface.mainWindow()
            act = mw.findChild(QAction, "mActionDataSourceManager") if mw is not None else None
        if act is not None:
            act.trigger()
            return True
        fn = getattr(self.iface, "openDataSourceManagerPage", None)
        if fn is not None:
            fn("ogr")
            return True
        return False


# ---- helpers ------------------------------------------------------------------------------------------------------
def make_layer(item, name=None):
    """A map layer for an item (not added to the project), or None when it does not open."""
    from qgis.core import QgsMeshLayer, QgsPointCloudLayer, QgsRasterLayer, QgsVectorLayer

    name = name or item["name"]
    lt, uri, prov = item["layer_type"], item["uri"], item["provider"]
    try:
        if lt == "vector":
            lyr = QgsVectorLayer(uri, name, prov or "ogr")
        elif lt == "raster":
            lyr = QgsRasterLayer(uri, name, prov or "gdal")
        elif lt == "mesh":
            lyr = QgsMeshLayer(uri, name, prov or "mdal")
        elif lt == "pointcloud":
            lyr = QgsPointCloudLayer(uri, name, prov or "pdal")
        elif lt == "vectortile":
            from qgis.core import QgsVectorTileLayer

            lyr = QgsVectorTileLayer(uri, name)
        elif lt == "tiledscene":
            from qgis.core import QgsTiledSceneLayer

            lyr = QgsTiledSceneLayer(uri, name, prov)
        else:
            return None
    except Exception:  # noqa: BLE001
        return None
    return lyr if lyr.isValid() else None


def recent_files():
    """The last data files added to maps (the Vault's list), those still there."""
    from ..vault import RECENT_KEY

    out = []
    for p in QgsSettings().value(RECENT_KEY, [], type=list) or []:
        if isinstance(p, str) and os.path.exists(p):
            out.append(p)
        if len(out) >= RECENT_SHOWN:
            break
    return out


def basemaps(plugin):
    """(key, name) of the basemaps the plugin offers (its BASEMAPS list)."""
    mod = sys.modules.get(type(plugin).__module__)
    table = getattr(mod, "BASEMAPS", None) or getattr(plugin, "BASEMAPS", None) or []
    return [(b[0], b[1]) for b in table]


def sources_from_mime(md):
    """File paths and web addresses in dropped / pasted data."""
    out = []
    if md is None:
        return out
    if md.hasUrls():
        for u in md.urls():
            if u.isLocalFile():
                out.append(os.path.normpath(u.toLocalFile()))
            elif u.scheme() in ("http", "https", "ftp"):
                out.append(u.toString().replace("%7B", "{").replace("%7D", "}"))
    if not out and md.hasText():
        for line in md.text().splitlines():
            s = line.strip().strip('"').strip("'")
            if not s:
                continue
            if s.lower().startswith("file:///"):
                from qgis.PyQt.QtCore import QUrl

                s = os.path.normpath(QUrl(s).toLocalFile())
            if detect.is_url(s) or os.path.exists(s):
                out.append(s)
    return out
