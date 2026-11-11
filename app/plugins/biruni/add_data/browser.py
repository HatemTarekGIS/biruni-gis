"""The data browser of the Add Data window (like the Add Data window of desktop GIS suites): places on one side -
the project folder, the Vault's folders, the home folders, the drives, recent files - and the contents of a folder
beside them: folders, and only the data the engine reads. Geodatabases, GeoPackages, archives, KML, GPX and CAD
drawings are one item that opens onto its layers (never a folder of raw .gdbtable files). Folders are listed and
files recognised in the background thread, so browsing never stops the window.

Double-click: a folder opens, a container unfolds, a layer (or a one-layer file) goes straight onto the map.
Ticking an item puts it in the "Ready to add" list; dragging items onto that list or onto the map works too."""
import os

from qgis.core import QgsMimeDataUtils, QgsProject, QgsSettings
from qgis.PyQt.QtCore import QDir, QMimeData, QSize, QStandardPaths, Qt, QUrl
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                                 QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QSplitter,
                                 QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import icons
from ..i18n import format_number, tr
from . import detect
from .kinds import icon_name, kind_name, tint

BROWSE_KEY = "biruni/add_data/browse_dir"
RECENT_PLACE = "::recent"
REFINE_MAX = 150            # files of a folder recognised one by one (kind, layer count) in the background
ROLE = Qt.ItemDataRole.UserRole
URI_TYPES = {"vector": "vector", "raster": "raster", "mesh": "mesh", "pointcloud": "pointcloud",
             "vectortile": "vector-tile", "tiledscene": "tiled-scene"}

# a first guess of a file's kind from its extension (refined by the engine in the background)
_GUESS = {}
for _k, _exts in (("shapes", ".shp .geojson .json .gml .tab .mif .fgb .geojsonl .topojson .osm .pbf"),
                  ("image", ".tif .tiff .jp2 .img .vrt .ecw .sid .nc .jpg .jpeg .png"),
                  ("elevation", ".asc .dem .hgt .bil"), ("table", ".csv .tsv .txt .dbf"),
                  ("spreadsheet", ".xlsx .xls .ods"), ("pointcloud", ".las .laz .copc .e57"), ("mesh", ".2dm"),
                  ("vectortile", ".pmtiles .vtpk .mbtiles"), ("project", ".qgz .qgs")):
    for _e in _exts.split():
        _GUESS[_e] = _k


def container_name(path):
    e = detect.ext_of(path)
    if e == ".gdb":
        return tr("Geodatabase")
    if e == ".gpkg":
        return "GeoPackage"
    if e == ".sqlite":
        return "SpatiaLite"
    if e in (".zip", ".kmz"):
        return kind_name("archive") if e == ".zip" else "KMZ"
    if e == ".kml":
        return "KML"
    if e == ".gpx":
        return kind_name("gps")
    return kind_name("cad")


def short_details(it):
    """Geometry type, feature count and size of a layer, short."""
    parts = []
    if it.get("geometry"):
        parts.append(it["geometry"])
    n = it.get("count")
    if n is not None:
        if it["kind"] in ("table", "spreadsheet"):
            parts.append(tr("1 row") if n == 1 else tr("{n} rows").format(n=format_number(n, 0)))
        elif it["kind"] == "pointcloud":
            parts.append(tr("{n} points").format(n=format_number(n, 0)))
        else:
            parts.append(tr("1 feature") if n == 1 else tr("{n} features").format(n=format_number(n, 0)))
    if it.get("pixels"):
        w, h = it["pixels"]
        parts.append(tr("{w} × {h} pixels").format(w=format_number(w, 0), h=format_number(h, 0)))
    if it.get("size") and it.get("inside", 1) == 1:
        parts.append(detect.human_size(it["size"]))
    return " · ".join(parts)


class DataTree(QTreeWidget):
    def __init__(self, browser):
        super().__init__()
        self.browser = browser
        self.setObjectName("AddDataTree")
        self.setColumnCount(3)
        self.setHeaderLabels([tr("Name"), tr("Kind"), tr("Details")])
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setExpandsOnDoubleClick(False)
        self.setUniformRowHeights(True)
        self.setIconSize(QSize(18, 18))
        self.setAllColumnsShowFocus(True)
        h = self.header()
        h.setStretchLastSection(True)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.resizeSection(0, 240)
        h.resizeSection(1, 110)

    def mimeTypes(self):  # noqa: N802
        return ["application/x-vnd.qgis.qgis.uri", "text/uri-list"]

    def mimeData(self, items):  # noqa: N802
        return self.browser.mime_for(items)

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key.Key_Backspace:
            self.browser.back()
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentItem() is not None:
            self.browser.activate(self.currentItem())
            return
        super().keyPressEvent(e)


class Browser(QWidget):
    def __init__(self, dlg):
        super().__init__()
        self.dlg = dlg
        self.setObjectName("AddDataBrowser")
        from ..vault.worker import Worker

        self.worker = Worker(self)
        self.worker.done.connect(self._done)
        self.current = ""
        self.history = []
        self.rows = {}           # row id -> {type, path, item, loaded, unreadable}
        self.jobs = {}           # job id -> row id (or "" for the listing)
        self.known = {}          # (provider, uri) -> item dict (for drag and drop)
        self._n = 0
        self._quiet = False
        self._expand_after = ""
        self.truncated = False
        self._build()
        self.refresh_places()
        self.navigate(self.start_folder(), remember=False)

    # -- building -----------------------------------------------------------------------------------------------
    def _build(self):
        t = self.dlg.tokens()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        bar = QHBoxLayout()
        bar.setSpacing(4)
        rtl = QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft
        self.back_button = QToolButton()
        self.back_button.setIcon(icons.icon("arrow-right" if rtl else "arrow-left", t["icon"], t["icon_disabled"]))
        self.back_button.setToolTip(tr("Back"))
        self.back_button.clicked.connect(self.back)
        bar.addWidget(self.back_button)
        self.up_button = QToolButton()
        self.up_button.setIcon(icons.icon("chevron-up", t["icon"], t["icon_disabled"]))
        self.up_button.setToolTip(tr("Up one folder"))
        self.up_button.clicked.connect(self.up)
        bar.addWidget(self.up_button)
        self.crumbs = QWidget()
        self.crumbs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.crumbs.setMinimumWidth(80)
        self.crumbs_layout = QHBoxLayout(self.crumbs)
        self.crumbs_layout.setContentsMargins(4, 0, 4, 0)
        self.crumbs_layout.setSpacing(0)
        bar.addWidget(self.crumbs, 1)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("Filter…"))
        self.filter.setClearButtonEnabled(True)
        self.filter.setMaximumWidth(160)
        self.filter.textChanged.connect(self.apply_filter)
        bar.addWidget(self.filter)
        lay.addLayout(bar)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        self.places = QListWidget()
        self.places.setObjectName("AddDataPlaces")
        self.places.setIconSize(QSize(16, 16))
        self.places.setMinimumWidth(130)
        self.places.itemClicked.connect(self._place_clicked)
        sl.addWidget(self.places, 1)
        self.add_folder_button = QPushButton(icons.icon("folder-plus", t["icon"]), tr("Add folder"))
        self.add_folder_button.setToolTip(tr("Add a folder to these places (also shown in the Vault)"))
        self.add_folder_button.clicked.connect(self.choose_place)
        sl.addWidget(self.add_folder_button)
        split.addWidget(side)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        self.tree = DataTree(self)
        self.tree.setMinimumWidth(250)
        self.tree.itemDoubleClicked.connect(lambda item, _col: self.activate(item))
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.itemChanged.connect(self._ticked)
        self.tree.itemSelectionChanged.connect(lambda: self.dlg.update_counts())
        rl.addWidget(self.tree, 1)
        self.info = QLabel()
        self.info.setProperty("dim", "true")
        self.info.setWordWrap(True)
        rl.addWidget(self.info)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([150, 380])
        lay.addWidget(split, 1)

    # -- places -------------------------------------------------------------------------------------------------
    def place_list(self):
        """[(section, [(label, path, icon)])] - what the places column shows."""
        out = []
        home = QgsProject.instance().homePath()
        if home and os.path.isdir(home):
            out.append((tr("This project"), [(tr("Project folder"), home, "folder-open")]))
        from ..vault.catalog import user_folders

        favs = [p for p in user_folders() if os.path.isdir(p)]
        if favs:
            out.append((tr("Folders"), [(os.path.basename(p.rstrip("/\\")) or p, p, "folder") for p in favs]))
        mine = [(tr("Home"), os.path.expanduser("~"), "folder")]
        for label, loc in ((tr("Documents"), QStandardPaths.StandardLocation.DocumentsLocation),
                           (tr("Desktop"), QStandardPaths.StandardLocation.DesktopLocation),
                           (tr("Downloads"), QStandardPaths.StandardLocation.DownloadLocation)):
            p = QStandardPaths.writableLocation(loc)
            if p and os.path.isdir(p):
                mine.append((label, os.path.normpath(p), "folder"))
        out.append((tr("This computer"), mine))
        drives = [os.path.normpath(d.absolutePath()) for d in QDir.drives()]
        if drives:
            out.append((tr("Drives"), [(d.rstrip("\\/") or d, d, "folders") for d in drives]))
        out.append((tr("Recent"), [(tr("Recent data"), RECENT_PLACE, "history")]))
        return out

    def refresh_places(self):
        t = self.dlg.tokens()
        self.places.clear()
        for section, rows in self.place_list():
            head = QListWidgetItem(section, self.places)
            head.setFlags(Qt.ItemFlag.NoItemFlags)
            f = QFont(head.font())
            f.setBold(True)
            f.setPointSizeF(max(7.0, f.pointSizeF() - 0.5))
            head.setFont(f)
            for label, path, ic in rows:
                it = QListWidgetItem(icons.icon(ic, t["accent"] if ic != "history" else t["icon"]), label,
                                     self.places)
                it.setData(ROLE, path)
                it.setToolTip(path if path != RECENT_PLACE else label)

    def place_paths(self):
        return [self.places.item(i).data(ROLE) for i in range(self.places.count()) if self.places.item(i).data(ROLE)]

    def _place_clicked(self, item):
        path = item.data(ROLE)
        if path:
            self.navigate(path)

    def choose_place(self):
        d = QFileDialog.getExistingDirectory(self, tr("Choose a folder"), self.current
                                             if os.path.isdir(self.current) else os.path.expanduser("~"))
        if d:
            self.add_place(d)

    def add_place(self, path):
        """Adds a folder to the places (the same list as the Vault's folders)."""
        from ..vault.catalog import FOLDERS_KEY, add_setting_path

        add_setting_path(FOLDERS_KEY, os.path.normpath(path))
        self.refresh_places()
        self.navigate(os.path.normpath(path))

    def start_folder(self):
        d = QgsSettings().value(BROWSE_KEY, "") or ""
        if d and os.path.isdir(d):
            return d
        home = QgsProject.instance().homePath()
        return home if home and os.path.isdir(home) else os.path.expanduser("~")

    # -- navigating ---------------------------------------------------------------------------------------------
    def navigate(self, path, remember=True):
        """Shows a folder (or the recent files). Returns at once: the folder is read in the background."""
        self._expand_after = ""
        if path != RECENT_PLACE:
            path = os.path.normpath(path)
            root = detect.gdb_root(path)
            if root:  # inside a geodatabase: show its folder with the geodatabase open
                self._expand_after = root
                path = os.path.dirname(root)
            elif os.path.isfile(path):
                self._expand_after = path
                path = os.path.dirname(path)
        if remember and self.current and self.current != path:
            self.history.append(self.current)
            del self.history[:-50]
        self.current = path
        if path != RECENT_PLACE and os.path.isdir(path):
            QgsSettings().setValue(BROWSE_KEY, path)
        self.worker.new_generation()
        self.jobs = {}
        self.rows = {}
        self.truncated = False
        self._quiet = True
        self.tree.clear()
        self._quiet = False
        self._crumbs()
        self.back_button.setEnabled(bool(self.history))
        self.up_button.setEnabled(path != RECENT_PLACE and os.path.dirname(path) != path)
        if path == RECENT_PLACE:
            self._fill_recent()
        else:
            self._submit("list", "", detect.list_dir, path)
        self._update_info()

    def back(self):
        if self.history:
            self.navigate(self.history.pop(), remember=False)

    def up(self):
        if self.current and self.current != RECENT_PLACE:
            parent = os.path.dirname(self.current)
            if parent and parent != self.current:
                self.navigate(parent)

    def _crumbs(self):
        lay = self.crumbs_layout
        while lay.count():
            w = lay.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        if self.current == RECENT_PLACE:
            chain = [(tr("Recent data"), RECENT_PLACE)]
        else:
            chain = []
            p = self.current
            while p:
                chain.insert(0, (os.path.basename(p.rstrip("/\\")) or p.rstrip("/\\") or p, p))
                parent = os.path.dirname(p)
                if parent == p:
                    break
                p = parent
        if len(chain) > 4:
            chain = [("…", chain[-5][1])] + chain[-4:]
        sep = "‹" if QApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft else "›"
        self.crumb_buttons = []
        for i, (label, path) in enumerate(chain):
            if i:
                s = QLabel(sep)
                s.setProperty("dim", "true")
                lay.addWidget(s)
            b = QToolButton()
            b.setObjectName("AddDataCrumb")
            b.setText(label if len(label) <= 28 else label[:26] + "…")
            b.setToolTip(path)
            b.clicked.connect(lambda _=False, p=path: self.navigate(p))
            lay.addWidget(b)
            self.crumb_buttons.append(b)
        lay.addStretch(1)

    # -- background jobs ----------------------------------------------------------------------------------------
    def _submit(self, what, row_id, fn, *args, priority=5):
        self._n += 1
        job = f"{what}:{self._n}"
        self.jobs[job] = row_id
        self.worker.submit(job, fn, *args, priority=priority)
        return job

    def busy(self):
        return bool(self.jobs)

    def _done(self, job, result, gen):
        if gen != self.worker.generation or job not in self.jobs:
            return
        row_id = self.jobs.pop(job)
        if isinstance(result, Exception):
            result = {"folders": [], "containers": [], "files": [], "error": str(result)} \
                if job.startswith("list:") else []
        if job.startswith("list:"):
            self._fill(result)
        else:
            item = self._item_of(row_id)
            if item is not None:
                if job.startswith("expand:"):
                    self._fill_container(item, result)
                else:
                    self._refine(item, result)
        self._update_info()

    def _item_of(self, row_id):
        def walk(item):
            if item.data(0, ROLE) == row_id:
                return item
            for i in range(item.childCount()):
                found = walk(item.child(i))
                if found is not None:
                    return found
            return None

        for i in range(self.tree.topLevelItemCount()):
            found = walk(self.tree.topLevelItem(i))
            if found is not None:
                return found
        return None

    # -- rows ---------------------------------------------------------------------------------------------------
    def _row(self, parent, kind_of_row, path, name, kind_text, details, icon, colour, item=None):
        self._n += 1
        rid = self._n
        self.rows[rid] = {"type": kind_of_row, "path": path, "item": item, "loaded": False, "unreadable": False}
        row = QTreeWidgetItem([name, kind_text, details])
        row.setData(0, ROLE, rid)
        row.setIcon(0, icons.icon(icon, colour))
        row.setToolTip(0, path)
        flags = row.flags()
        if kind_of_row in ("file", "container", "layer"):
            flags |= Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled
        else:
            flags &= ~Qt.ItemFlag.ItemIsDragEnabled
        if kind_of_row == "info":
            flags &= ~(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
        row.setFlags(flags)
        self._quiet = True
        if kind_of_row in ("file", "container", "layer"):
            on = self.dlg.has_card_for(self.rows[rid])
            self.rows[rid]["ticked"] = on
            row.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        if kind_of_row == "container":
            row.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        if parent is None:
            self.tree.addTopLevelItem(row)
        else:
            parent.addChild(row)
        self._quiet = False
        if item is not None:
            self.known[(item["provider"], item["uri"])] = item
        return row

    def data(self, row):
        return self.rows.get(row.data(0, ROLE)) if row is not None else None

    def _fill(self, listing):
        t, dark = self.dlg.tokens(), self.dlg.dark()
        self.truncated = bool(listing.get("truncated"))
        if listing.get("error"):
            self._row(None, "info", self.current, tr("This folder cannot be opened."), "", "", "alert-triangle",
                      t["amber"])
            return
        for p in listing["folders"]:
            self._row(None, "folder", p, os.path.basename(p), tr("Folder"), "", "folder", tint("archive", dark))
        for p in listing["containers"]:
            self._row(None, "container", p, os.path.basename(p), container_name(p), "", "database",
                      tint("project", dark))
        refine = []
        for p in listing["files"]:
            kind = _GUESS.get(detect.ext_of(p), "shapes")
            row = self._row(None, "file", p, os.path.basename(p), kind_name(kind),
                            detect.human_size(detect.dataset_size(p)) if len(listing["files"]) < 400 else "",
                            icon_name(kind), tint(kind, dark))
            refine.append(row)
        for row in refine[:REFINE_MAX]:
            self._submit("refine", row.data(0, ROLE), detect.detect, self.rows[row.data(0, ROLE)]["path"],
                         self.dlg.project_crs, priority=9)
        if not (listing["folders"] or listing["containers"] or listing["files"]):
            self._row(None, "info", self.current, tr("No map data in this folder."), "", "", "info-circle",
                      t["text_dim"])
        if self._expand_after:
            want = os.path.normcase(self._expand_after)
            for i in range(self.tree.topLevelItemCount()):
                row = self.tree.topLevelItem(i)
                d = self.data(row)
                if d and os.path.normcase(d["path"]) == want:
                    self.tree.setCurrentItem(row)
                    if d["type"] == "container":
                        row.setExpanded(True)
                    break
            self._expand_after = ""
        self.apply_filter(self.filter.text())

    def _fill_recent(self):
        from ..vault import RECENT_KEY

        t, dark = self.dlg.tokens(), self.dlg.dark()
        n = 0
        for p in QgsSettings().value(RECENT_KEY, [], type=list) or []:
            if not isinstance(p, str) or not os.path.exists(p):
                continue
            e = detect.ext_of(p)
            if e in detect.CONTAINER_EXT:
                row = self._row(None, "container", p, os.path.basename(p), container_name(p), os.path.dirname(p),
                                "database", tint("project", dark))
            else:
                kind = _GUESS.get(e, "shapes")
                row = self._row(None, "file", p, os.path.basename(p), kind_name(kind), os.path.dirname(p),
                                icon_name(kind), tint(kind, dark))
                self._submit("refine", row.data(0, ROLE), detect.detect, p, self.dlg.project_crs, priority=9)
            n += 1
        if not n:
            self._row(None, "info", "", tr("Nothing added yet."), "", "", "info-circle", t["text_dim"])

    def _expanded(self, row):
        d = self.data(row)
        if not d or d["type"] != "container" or d["loaded"] or d.get("loading"):
            return
        d["loading"] = True
        t = self.dlg.tokens()
        self._row(row, "info", d["path"], tr("Reading…"), "", "", "clock-hour-4", t["text_dim"])
        self._submit("expand", row.data(0, ROLE), detect.detect, d["path"], self.dlg.project_crs, priority=0)

    def _layer_rows(self, row, items):
        dark = self.dlg.dark()
        for it in items:
            self._row(row, "layer", it["path"], it["name"], kind_name(it["kind"]), short_details(it),
                      icon_name(it["kind"]), tint(it["kind"], dark), item=it)

    def _fill_container(self, row, items):
        d = self.data(row)
        self._quiet = True
        row.takeChildren()
        self._quiet = False
        d["loaded"] = True
        d["loading"] = False
        readable = [i for i in items if i["readable"] and i["layer_type"] not in ("project", "service")]
        if readable:
            self._layer_rows(row, readable)
            row.setText(2, tr("1 layer") if len(readable) == 1 else
                        tr("{n} layers").format(n=format_number(len(readable), 0)))
        else:
            t = self.dlg.tokens()
            self._row(row, "info", d["path"], tr("No layers that can be read."), "", "", "info-circle",
                      t["text_dim"])
        self.apply_filter(self.filter.text())

    def _refine(self, row, items):
        d = self.data(row)
        readable = [i for i in items if i["readable"]]
        if not readable:
            d["unreadable"] = True
            row.setHidden(True)
            return
        dark = self.dlg.dark()
        if len(readable) == 1:
            it = readable[0]
            d["item"] = it
            self.known[(it["provider"], it["uri"])] = it
            row.setText(1, kind_name(it["kind"]))
            row.setText(2, short_details(it))
            row.setIcon(0, icons.icon(icon_name(it["kind"]), tint(it["kind"], dark)))
            return
        # several layers in one file (a KML with folders, a DXF of points and lines …): a container
        d["type"] = "container"
        d["loaded"] = True
        row.setIcon(0, icons.icon("database", tint("project", dark)))
        row.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        self._layer_rows(row, readable)
        row.setText(2, tr("{n} layers").format(n=format_number(len(readable), 0)))

    # -- filter and info ----------------------------------------------------------------------------------------
    def apply_filter(self, text):
        words = (text or "").lower().split()

        def hit(row):
            name = row.text(0).lower()
            return all(w in name for w in words)

        for i in range(self.tree.topLevelItemCount()):
            row = self.tree.topLevelItem(i)
            d = self.data(row) or {}
            child_hit = False
            for k in range(row.childCount()):
                c = row.child(k)
                show = not words or hit(c)
                c.setHidden(not show)
                child_hit = child_hit or (show and bool(words))
            row.setHidden(bool(d.get("unreadable")) or not (not words or hit(row) or child_hit))
        self._update_info()

    def visible_names(self):
        return [self.tree.topLevelItem(i).text(0) for i in range(self.tree.topLevelItemCount())
                if not self.tree.topLevelItem(i).isHidden()]

    def _update_info(self):
        listing = any(j.startswith("list:") for j in self.jobs)
        if listing:
            self.info.setText(tr("Reading the folder…"))
            return
        n = sum(1 for i in range(self.tree.topLevelItemCount())
                if not self.tree.topLevelItem(i).isHidden()
                and (self.data(self.tree.topLevelItem(i)) or {}).get("type") != "info")
        text = tr("1 item") if n == 1 else tr("{n} items").format(n=format_number(n, 0))
        if self.truncated:
            text += " " + tr("Only the first {n} are shown.").format(n=format_number(detect.LIST_MAX, 0))
        text += " · " + tr("Double-click adds a layer to the map; tick to add several.")
        self.info.setText(text)

    # -- using the rows -----------------------------------------------------------------------------------------
    def activate(self, row):
        """Double-click: open a folder, unfold a container, add a layer or a one-layer file to the map."""
        d = self.data(row)
        if not d:
            return
        if d["type"] == "folder":
            self.navigate(d["path"])
        elif d["type"] == "container":
            row.setExpanded(not row.isExpanded())
        elif d["type"] == "layer" or (d["type"] == "file" and d.get("item")):
            it = d["item"]
            if it["layer_type"] == "project":
                self.dlg.open_project(it["path"])
            else:
                self.dlg.add_items([it])
        elif d["type"] == "file":
            self.dlg.add_paths([d["path"]], auto_add=True)

    def _ticked(self, row, col):
        if self._quiet or col != 0:
            return
        d = self.data(row)
        if not d or d["type"] not in ("file", "container", "layer"):
            return
        on = row.checkState(0) == Qt.CheckState.Checked
        if on == d.get("ticked", False):
            return  # another change of the row (its kind or details filled in), not the tick
        d["ticked"] = on
        if on:
            self.send(d)
        else:
            self.dlg.remove_cards_for(d)

    def send(self, d):
        """Puts a row in the Ready to add list."""
        if self.dlg.has_card_for(d):
            return
        if d.get("item") is not None and d["type"] in ("layer", "file"):
            self.dlg.add_item_card(d["item"])
        else:
            self.dlg.add_paths([d["path"]])

    def selected_rows(self):
        out = []
        for row in self.tree.selectedItems():
            d = self.data(row)
            if d and d["type"] in ("file", "container", "layer") and not row.isHidden():
                out.append(d)
        return out

    def send_selection(self):
        rows = self.selected_rows()
        for d in rows:
            self.send(d)
        self.tree.clearSelection()
        return len(rows)

    def sync_ticks(self):
        """The ticks follow the Ready to add list."""
        self._quiet = True

        def walk(row):
            d = self.data(row)
            if d and d["type"] in ("file", "container", "layer"):
                on = self.dlg.has_card_for(d)
                d["ticked"] = on
                want = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
                if row.checkState(0) != want:
                    row.setCheckState(0, want)
            for k in range(row.childCount()):
                walk(row.child(k))

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        self._quiet = False

    def mime_for(self, rows):
        """Dragged rows: engine layer URIs (the map and the list take them) and file addresses."""
        uris, urls = [], []
        for row in rows:
            d = self.data(row)
            if not d or d["type"] not in ("file", "container", "layer"):
                continue
            it = d.get("item")
            if it is not None and d["type"] in ("layer", "file") and it["layer_type"] in URI_TYPES:
                u = QgsMimeDataUtils.Uri()
                u.layerType = URI_TYPES[it["layer_type"]]
                u.providerKey = it["provider"]
                u.name = it["name"]
                u.uri = it["uri"]
                uris.append(u)
            else:
                urls.append(QUrl.fromLocalFile(d["path"]))
        md = QgsMimeDataUtils.encodeUriList(uris) if uris else QMimeData()
        if urls:
            md.setUrls(urls)
        return md
