"""Biruni toolbox panel: searchable, categorised tools with favourites and
recently used tools, and a description pane - like a geoprocessing pane."""
import html

from qgis.core import QgsApplication, QgsSettings
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtWidgets import (QDockWidget, QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QSplitter,
                                 QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import icons
from .i18n import tr
from .toolbox_catalog import CATEGORIES, COMMANDS, LABELS

FAV_KEY = "biruni/toolbox/favorites"
RECENT_KEY = "biruni/toolbox/recent"
ROLE = Qt.ItemDataRole.UserRole


def toolbox_qss(t):
    return f"""
#BiruniToolbox QTreeWidget {{ border: none; background: {t['base']}; }}
#BiruniToolbox QTreeWidget::item {{ padding: 3px 2px; }}
#BiruniToolbox QLineEdit {{ border: 1px solid {t['border']}; border-radius: 6px; padding: 5px 8px;
                           background: {t['base']}; }}
#BiruniToolbox QLineEdit:focus {{ border-color: {t['accent']}; }}
#ToolboxInfo {{ background: {t['panel']}; border-top: 1px solid {t['border']}; }}
#ToolboxInfo QLabel {{ color: {t['text']}; }}
#ToolboxRun {{ background: {t['accent']}; color: {t['accent_text']}; border: none; border-radius: 5px;
              padding: 6px 16px; font-weight: 600; }}
#ToolboxRun:hover {{ background: {t['accent_hover']}; }}
#ToolboxRun:disabled {{ background: {t['mid']}; }}
#ToolboxAll {{ border: none; color: {t['accent']}; background: transparent; padding: 4px; }}
"""


class Tool:
    def __init__(self, key, name, help_text, icon_name, english=""):
        self.key, self.name, self.help, self.icon, self.english = key, name, help_text, icon_name, english


class ToolboxDock(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("Toolbox"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.setObjectName("BiruniToolbox")
        self.tools = {}
        body = QWidget()
        body.setObjectName("BiruniToolbox")
        v = QVBoxLayout(body)
        v.setContentsMargins(6, 6, 6, 0)
        v.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Search tools…"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.populate)
        self.search.returnPressed.connect(self._run_first_match)
        v.addWidget(self.search)

        split = QSplitter(Qt.Orientation.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIconSize(QSize(18, 18))
        self.tree.setIndentation(14)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemActivated.connect(lambda item, _c: self.run(item.data(0, ROLE)))
        self.tree.currentItemChanged.connect(lambda cur, _p: self._describe(cur.data(0, ROLE) if cur else None))
        split.addWidget(self.tree)

        info = QWidget()
        info.setObjectName("ToolboxInfo")
        iv = QVBoxLayout(info)
        iv.setContentsMargins(10, 8, 10, 10)
        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.info_label.setTextFormat(Qt.TextFormat.RichText)
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        iv.addWidget(self.info_label, 1)
        row = QHBoxLayout()
        self.fav_btn = QPushButton()
        self.fav_btn.setObjectName("ToolboxAll")
        self.fav_btn.clicked.connect(lambda: self.toggle_favorite(self._current_key()))
        row.addWidget(self.fav_btn)
        row.addStretch(1)
        self.run_btn = QPushButton(tr("Open tool"))
        self.run_btn.setObjectName("ToolboxRun")
        self.run_btn.clicked.connect(lambda: self.run(self._current_key()))
        row.addWidget(self.run_btn)
        iv.addLayout(row)
        split.addWidget(info)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        v.addWidget(split, 1)

        self.all_btn = QPushButton()
        self.all_btn.setObjectName("ToolboxAll")
        self.all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_btn.clicked.connect(self._show_qgis_toolbox)
        v.addWidget(self.all_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.setWidget(body)
        self.setMinimumWidth(280)

    # ---- data ------------------------------------------------------------
    def load(self):
        reg = QgsApplication.processingRegistry()
        self.tools = {}
        for _key, _title, icon_name, entries in CATEGORIES:
            for e in entries:
                if e in self.tools:
                    continue
                if e.startswith("cmd:"):
                    label, ic, help_text, _m = COMMANDS[e[4:]]
                    self.tools[e] = Tool(e, tr(label), tr(help_text), ic, label)
                    continue
                alg = reg.algorithmById(e)
                if alg is None:
                    continue
                help_text = alg.shortDescription() or alg.shortHelpString() or ""
                label = LABELS.get(e)
                if label:
                    self.tools[e] = Tool(e, tr(label), help_text, icon_name, f"{label} {alg.displayName()} {alg.name()}")
                else:
                    self.tools[e] = Tool(e, alg.displayName(), help_text, icon_name, alg.name())
        n = len(reg.algorithms())
        self.all_btn.setText(tr("All tools ({n}) …").format(n=n))
        self.populate()

    def _favorites(self):
        return [k for k in (QgsSettings().value(FAV_KEY, [], type=list) or []) if k in self.tools]

    def _recent(self):
        return [k for k in (QgsSettings().value(RECENT_KEY, [], type=list) or []) if k in self.tools]

    # ---- view ------------------------------------------------------------
    def populate(self, *_):
        t = self.plugin.theme.tokens()
        text = self.search.text().strip().lower()
        self.tree.clear()

        def matches(tool):
            if not text:
                return True
            hay = f"{tool.name} {tool.english} {tool.key} {tool.help}".lower()
            return all(word in hay for word in text.split())

        def add_category(title, icon_name, keys, expanded):
            keys = [k for k in keys if k in self.tools and matches(self.tools[k])]
            if not keys:
                return
            cat = QTreeWidgetItem(self.tree, [title])
            cat.setIcon(0, icons.icon(icon_name, t["accent"]))
            f = cat.font(0)
            f.setBold(True)
            cat.setFont(0, f)
            cat.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for k in keys:
                tool = self.tools[k]
                it = QTreeWidgetItem(cat, [tool.name])
                it.setData(0, ROLE, k)
                it.setToolTip(0, tool.help)
                it.setIcon(0, icons.icon(tool.icon, t["icon"]))
            cat.setExpanded(expanded or bool(text))

        add_category(tr("Favorites"), "star", self._favorites(), True)
        add_category(tr("Recent"), "clock-hour-4", self._recent(), True)
        for _key, title, icon_name, entries in CATEGORIES:
            add_category(tr(title), icon_name, entries, False)
        self._describe(None)

    def _describe(self, key):
        tool = self.tools.get(key)
        self.run_btn.setEnabled(tool is not None)
        self.fav_btn.setVisible(tool is not None)
        if tool is None:
            self.info_label.setText(f"<span>{html.escape(tr('Choose a tool to see what it does.'))}</span>")
            return
        fav = key in self._favorites()
        self.fav_btn.setText(("★ " + tr("Remove from favorites")) if fav else ("☆ " + tr("Add to favorites")))
        help_text = tool.help if "<" in tool.help else html.escape(tool.help)
        self.info_label.setText(f"<b>{html.escape(tool.name)}</b><br><span>{help_text}</span>")

    def _current_key(self):
        item = self.tree.currentItem()
        return item.data(0, ROLE) if item else None

    def _run_first_match(self):
        it = self.tree.topLevelItem(0)
        if it is not None and it.childCount():
            self.run(it.child(0).data(0, ROLE))

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        key = item.data(0, ROLE) if item else None
        if not key:
            return
        m = QMenu(self)
        m.addAction(tr("Open tool"), lambda: self.run(key))
        fav = key in self._favorites()
        m.addAction(tr("Remove from favorites") if fav else tr("Add to favorites"),
                    lambda: self.toggle_favorite(key))
        m.exec(self.tree.viewport().mapToGlobal(pos))

    def _show_qgis_toolbox(self):
        d = self.plugin.runner.dock("ProcessingToolbox")
        if d is not None:
            d.show()
            d.raise_()

    def refresh_theme(self):
        self.widget().setStyleSheet(toolbox_qss(self.plugin.theme.tokens()))
        if self.tools:
            self.populate()

    # ---- actions -------------------------------------------------------
    def toggle_favorite(self, key):
        if not key:
            return
        favs = self._favorites()
        favs = [k for k in favs if k != key] if key in favs else favs + [key]
        QgsSettings().setValue(FAV_KEY, favs)
        self.populate()

    def run(self, key):
        if not key or key not in self.tools:
            return
        recent = [key] + [k for k in self._recent() if k != key]
        QgsSettings().setValue(RECENT_KEY, recent[:8])
        if key.startswith("cmd:"):
            target = COMMANDS[key[4:]][3]
            if hasattr(self.plugin.project, target):
                getattr(self.plugin.project, target)()
            else:  # a command of the Biruni catalogue
                from .commands import CATALOG

                self.plugin.runner.execute(CATALOG[target])
        else:
            self.plugin.runner.open_algorithm(key)
