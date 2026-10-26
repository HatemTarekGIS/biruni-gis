"""Start page shown over the map while the project is empty. Replaces the QGIS
welcome screen (which is hidden whenever QGIS wants to show it)."""
import os

from qgis.core import Qgis, QgsProject
from qgis.PyQt.QtCore import QEvent, QObject, QSize, Qt, QTimer
from qgis.PyQt.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
                                 QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from . import i18n, icons
from .commands import CATALOG
from .i18n import tr
from .version import APP_NAME, VERSION


def start_qss(t):
    return f"""
#BiruniStart {{ background: {t['start_bg']}; }}
#BiruniStart QLabel {{ background: transparent; color: {t['text']}; }}
#StartTitle {{ font-size: 22pt; font-weight: 700; }}
#StartSubtitle, #StartFooter {{ color: {t['text_dim']}; }}
#StartSection {{ font-size: 11pt; font-weight: 600; padding-top: 6px; }}
QFrame#StartCard {{
    background: {t['card']}; border: 1px solid {t['border']}; border-radius: 10px;
}}
QFrame#StartCard:hover {{ border: 1px solid {t['accent']}; background: {t['card_hover']}; }}
#StartCardTitle {{ font-size: 10.5pt; font-weight: 600; }}
#StartCardText {{ color: {t['text_dim']}; }}
QFrame#StartRecent {{ background: {t['card']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QPushButton#RecentItem {{
    text-align: left; border: none; border-radius: 6px; padding: 7px 10px;
    background: transparent; color: {t['text']};
}}
QPushButton#RecentItem:hover {{ background: {t['hover']}; }}
QPushButton#RecentItem:disabled {{ color: {t['text_disabled']}; }}
QPushButton#StartLink {{
    border: none; background: transparent; color: {t['text_dim']}; padding: 4px 8px; border-radius: 5px;
}}
QPushButton#StartLink:hover {{ color: {t['accent']}; background: {t['hover']}; }}
QPushButton#StartLink:checked {{ color: {t['accent']}; font-weight: 600; }}
QScrollArea#StartScroll, QScrollArea#StartScroll > QWidget > QWidget {{ background: transparent; border: none; }}
"""


class Card(QFrame):
    def __init__(self, icon_name, title, text, on_click):
        super().__init__()
        self.setObjectName("StartCard")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(136)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._on_click = on_click
        self.icon_name = icon_name
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(4)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(30, 30)
        v.addWidget(self.icon_label)
        v.addSpacing(4)
        t = QLabel(title)
        t.setObjectName("StartCardTitle")
        v.addWidget(t)
        d = QLabel(text)
        d.setObjectName("StartCardText")
        d.setWordWrap(True)
        v.addWidget(d)
        v.addStretch(1)

    def refresh(self, tokens):
        self.icon_label.setPixmap(icons.icon(self.icon_name, tokens["accent"]).pixmap(QSize(30, 30)))

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self._on_click()
        super().mouseReleaseEvent(ev)


class StartPage(QWidget):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.iface = plugin.iface
        self.mw = self.iface.mainWindow()
        self.setObjectName("BiruniStart")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.welcome = None
        self.cards = []
        self._filter = _Filter(self)
        self._build()

    # ---- UI ------------------------------------------------------------
    def _build(self):
        scroll = QScrollArea(self)
        scroll.setObjectName("StartScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        host = QWidget()
        scroll.setWidget(host)
        hv = QHBoxLayout(host)
        hv.addStretch(1)
        content = QWidget()
        content.setMaximumWidth(1080)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        hv.addWidget(content, 8)
        hv.addStretch(1)

        v = QVBoxLayout(content)
        v.setContentsMargins(24, 40, 24, 24)
        v.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(16)
        self.logo = QLabel()
        self.logo.setFixedSize(64, 64)
        self.logo.setPixmap(icons.logo_pixmap(64))
        head.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignVCenter)
        tv = QVBoxLayout()
        tv.setSpacing(2)
        title = QLabel(APP_NAME)
        title.setObjectName("StartTitle")
        sub = QLabel(tr("Free and open-source GIS. Measure, map and analyse the world."))
        sub.setObjectName("StartSubtitle")
        tv.addWidget(title)
        tv.addWidget(sub)
        head.addLayout(tv, 1)
        v.addLayout(head)
        v.addSpacing(18)

        sec = QLabel(tr("Start"))
        sec.setObjectName("StartSection")
        v.addWidget(sec)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        specs = [
            ("folder-plus", tr("New project"),
             tr("Choose a name and a place. Biruni creates a project folder that keeps the map and all of its "
                "data together."), self._new_project),
            ("file-dots", tr("Temporary map"),
             tr("Start working right away without saving. You can save it as a project folder later."),
             self._temporary),
            ("folder-open", tr("Open project"), tr("Open a saved .qgz or .qgs project."), self._open_project),
            ("database-plus", tr("Open data"),
             tr("Look at a Shapefile, GeoPackage, File GDB, DXF, CSV or raster in a temporary map."), self._add_data),
        ]
        specs += [
            ("file-arrow-right", tr("ArcGIS project"),
             tr("Open an ArcGIS Pro file (.aprx, .mapx, .lyrx) or an ArcMap document (.mxd) with its layers."),
             lambda: self.plugin.runner.execute(CATALOG["open_arcgis"])),
            ("table-import", tr("Points from Excel"),
             tr("A spreadsheet with coordinates becomes points - the columns are found by themselves."),
             lambda: (self.hide_page(), self.plugin.runner.execute(CATALOG["smart_table"]))),
            ("world", tr("Sample map"), tr("Practise on a ready world map: countries, cities, rivers and lakes."),
             lambda: self.plugin.runner.execute(CATALOG["sample_data"])),
            ("school", tr("Learn Biruni GIS"),
             tr("Short guided lessons, and where to find the tools you know from ArcGIS."),
             lambda: (self.hide_page(), self.plugin.runner.execute(CATALOG["learn"]))),
        ]
        for i, (ic, t, d, fn) in enumerate(specs):
            c = Card(ic, t, d, fn)
            self.cards.append(c)
            grid.addWidget(c, i // 4, i % 4)
        v.addLayout(grid)
        v.addSpacing(14)

        rs = QLabel(tr("Recent projects"))
        rs.setObjectName("StartSection")
        v.addWidget(rs)
        self.recent_box = QFrame()
        self.recent_box.setObjectName("StartRecent")
        self.recent_layout = QVBoxLayout(self.recent_box)
        self.recent_layout.setContentsMargins(6, 6, 6, 6)
        self.recent_layout.setSpacing(0)
        v.addWidget(self.recent_box)
        v.addStretch(1)

        foot = QHBoxLayout()
        engine = Qgis.version().split("-")[0]
        info = QLabel(tr("Version {v} · Based on QGIS {q} · Free and open source (GPL)").format(v=VERSION, q=engine))
        info.setObjectName("StartFooter")
        foot.addWidget(info)
        about = QPushButton(tr("About"))
        about.setObjectName("StartLink")
        about.setCursor(Qt.CursorShape.PointingHandCursor)
        about.clicked.connect(self.plugin.branding.show_about)
        foot.addWidget(about)
        foot.addStretch(1)
        self.lang_buttons = []
        for code, name in i18n.LANGUAGES:
            b = QPushButton(name)
            b.setObjectName("StartLink")
            b.setCheckable(True)
            b.setChecked(code == i18n.current_code())
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, c=code: self.plugin.change_language(c))
            foot.addWidget(b)
            self.lang_buttons.append(b)
        self.theme_buttons = []
        for mode, ic, tip in (("light", "sun", tr("Light")), ("dark", "moon", tr("Dark")), ("system", "device-desktop", tr("Follow Windows"))):
            b = QToolButton()
            b.setObjectName("RibbonIcon")
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b._icon_name, b._mode = ic, mode
            b.clicked.connect(lambda _=False, m=mode: self.plugin.theme.set_mode(m))
            foot.addWidget(b)
            self.theme_buttons.append(b)
        v.addLayout(foot)

    def refresh_recent(self):
        while self.recent_layout.count():
            w = self.recent_layout.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        t = self.plugin.theme.tokens()
        menu = self.mw.findChild(QMenu, "mRecentProjectsMenu")
        items = []
        if menu is not None:
            for a in menu.actions():
                path = a.data()
                if isinstance(path, str) and path:
                    items.append((a.text().replace("&", ""), path, os.path.exists(path)))
        if not items:
            empty = QLabel(tr("No recent projects yet. Projects you save will appear here."))
            empty.setObjectName("StartSubtitle")
            empty.setContentsMargins(10, 10, 10, 10)
            self.recent_layout.addWidget(empty)
            return
        for text, path, ok in items[:8]:
            b = QPushButton(icons.icon("map", t["icon"], t["icon_disabled"]), "  " + text)
            b.setObjectName("RecentItem")
            b.setIconSize(QSize(16, 16))
            b.setEnabled(ok)
            b.setToolTip(path)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, p=path: self._open_recent(p))
            self.recent_layout.addWidget(b)

    def refresh_theme(self):
        t = self.plugin.theme.tokens()
        self.setStyleSheet(start_qss(t) + "QToolButton#RibbonIcon { border: none; border-radius: 5px; padding: 4px; }"
                           f"QToolButton#RibbonIcon:hover {{ background: {t['hover']}; }}"
                           f"QToolButton#RibbonIcon:checked {{ background: {t['accent_soft']}; }}")
        for c in self.cards:
            c.refresh(t)
        mode = self.plugin.theme.mode()
        for b in self.theme_buttons:
            b.setIcon(icons.icon(b._icon_name, t["accent"] if b._mode == mode else t["icon"]))
        self.refresh_recent()

    # ---- actions -------------------------------------------------------
    def _new_project(self):
        self.plugin.project.new_project()

    def _temporary(self):
        self.plugin.project.start_temporary()

    def _open_project(self):
        self.plugin.runner.execute(CATALOG["open"])

    def _add_data(self):
        self.plugin.runner.execute(CATALOG["add_data"])

    def _open_recent(self, path):
        self.iface.addProject(path)

    # ---- visibility ----------------------------------------------------
    def install(self):
        central = self.mw.centralWidget()
        self.setParent(central)
        central.installEventFilter(self._filter)
        self.adopt_welcome()
        prj = QgsProject.instance()
        prj.layersAdded.connect(self._on_content)
        prj.readProject.connect(self._on_content)
        prj.cleared.connect(self._on_cleared)
        self.refresh_theme()
        self.sync()

    def unload(self):
        prj = QgsProject.instance()
        for sig, slot in ((prj.layersAdded, self._on_content), (prj.readProject, self._on_content),
                          (prj.cleared, self._on_cleared)):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass
        central = self.mw.centralWidget()
        if central is not None:
            central.removeEventFilter(self._filter)
        if self.welcome is not None:
            self.welcome.removeEventFilter(self._filter)
        self.hide()
        self.setParent(None)
        self.deleteLater()

    def adopt_welcome(self):
        """Find the QGIS welcome screen (created lazily by QGIS) and keep it hidden."""
        if self.welcome is not None:
            return
        for w in self.mw.centralWidget().findChildren(QWidget):
            if w.metaObject().className() == "QgsWelcomeScreen":
                self.welcome = w
                w.installEventFilter(self._filter)
                if w.isVisible():
                    w.hide()
                    if self.project_is_empty():
                        self.show_page()
                break

    def project_is_empty(self):
        prj = QgsProject.instance()
        return not prj.mapLayers() and not prj.fileName()

    def sync(self):
        self.adopt_welcome()
        if self.project_is_empty():
            self.show_page()
        else:
            self.hide_page()

    def show_page(self):
        central = self.mw.centralWidget()
        self.setGeometry(central.rect())
        self.refresh_recent()
        self.show()
        self.raise_()

    def hide_page(self):
        self.hide()

    def _on_content(self, *args):
        self.hide_page()

    def _on_cleared(self):
        self.hide_page()


class _Filter(QObject):
    """Keeps the start page sized to the central widget and swallows the QGIS
    welcome screen."""

    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def eventFilter(self, obj, ev):
        page = self.page
        central = page.mw.centralWidget()
        if obj is central and ev.type() == QEvent.Type.Resize:
            page.setGeometry(obj.rect())
        elif obj is central and ev.type() == QEvent.Type.ChildAdded and page.welcome is None:
            QTimer.singleShot(0, page.adopt_welcome)
        elif obj is page.welcome and ev.type() == QEvent.Type.Show:
            QTimer.singleShot(0, obj.hide)
            QTimer.singleShot(60, page.show_page)
        return False
