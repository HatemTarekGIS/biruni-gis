"""The Map Studio window: 1 What is the map for? → 2 Check (preview, colour theme, map doctor) → 3 Export.

A live preview of the page follows every choice; "Open in layout designer" hands the layout over for manual
polish at any step."""
import os

from qgis.core import QgsLayoutExporter, QgsProject, QgsSettings
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
                                 QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QStackedWidget,
                                 QToolButton, QVBoxLayout, QWidget)

from .. import icons
from ..i18n import tr
from . import auto_layout, intents, optional, outputs

ACCENT = "#2563EB"
PREVIEW_PX = 460
DIR_KEY = "biruni/studio/folder"
OUTPUTS_KEY = "biruni/studio/outputs"


class StudioDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(getattr(plugin, "mw", None))
        self.plugin = plugin
        self.project = QgsProject.instance()
        self.layout = None
        self.purpose = None
        self.themes = optional("themes")
        self.doctor = optional("doctor")
        self.setWindowTitle(tr("Map Studio"))
        self.resize(980, 640)
        root = QVBoxLayout(self)

        head = QHBoxLayout()
        self.steps = [QLabel(t) for t in (tr("1  What is it for?"), tr("2  Check"), tr("3  Export"))]
        for s in self.steps:
            head.addWidget(s)
        head.addStretch(1)
        root.addLayout(head)

        body = QHBoxLayout()
        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_purpose())
        self.stack.addWidget(self._page_check())
        self.stack.addWidget(self._page_export())
        body.addWidget(self.stack, 3)
        side = QVBoxLayout()
        self.preview = QLabel(tr("Choose what the map is for to see it here."))
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(PREVIEW_PX, int(PREVIEW_PX * 0.72))
        self.preview.setStyleSheet("QLabel { background: #F3F5F7; border: 1px solid #D6DBE0; }")
        side.addWidget(self.preview, 1)
        self.info = QLabel()
        self.info.setWordWrap(True)
        side.addWidget(self.info)
        body.addLayout(side, 2)
        root.addLayout(body, 1)

        nav = QHBoxLayout()
        self.designer_btn = QPushButton(tr("Open in layout designer"))
        self.designer_btn.clicked.connect(self.open_designer)
        nav.addWidget(self.designer_btn)
        nav.addStretch(1)
        self.back_btn = QPushButton(tr("Back"))
        self.back_btn.clicked.connect(lambda: self.go(self.stack.currentIndex() - 1))
        self.next_btn = QPushButton(tr("Next"))
        self.next_btn.clicked.connect(lambda: self.go(self.stack.currentIndex() + 1))
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.reject)
        for b in (self.back_btn, self.next_btn, close):
            nav.addWidget(b)
        root.addLayout(nav)
        self.go(0)

    # ---- step 1 --------------------------------------------------------------------------------------------
    def _page_purpose(self):
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Title")))
        self.title = QLineEdit(auto_layout.default_title(self.project, auto_layout.drawn_layers(self.project)))
        self.title.editingFinished.connect(self.rebuild)
        row.addWidget(self.title, 1)
        v.addLayout(row)
        grid = QGridLayout()
        self.cards = {}
        for k, iid in enumerate(intents.ORDER):
            intent = intents.get(iid)
            b = QToolButton()
            b.setText(tr(intent.label))
            b.setToolTip(tr(intent.help))
            try:
                b.setIcon(icons.icon(intent.icon, ACCENT))
            except OSError:  # icon not collected into resources yet
                pass
            b.setIconSize(QSize(34, 34))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setCheckable(True)
            b.setMinimumSize(150, 86)
            b.clicked.connect(lambda _=False, i=iid: self.choose(i, advance=True))
            grid.addWidget(b, k // 4, k % 4)
            self.cards[iid] = b
        v.addLayout(grid)
        v.addStretch(1)
        return w

    # ---- step 2 --------------------------------------------------------------------------------------------
    def _page_check(self):
        w = QWidget()
        v = QVBoxLayout(w)
        if self.themes is not None and getattr(self.themes, "THEMES", None):
            row = QHBoxLayout()
            row.addWidget(QLabel(tr("Colour theme")))
            self.theme = QComboBox()
            self.theme.addItem(tr("Keep the current colours"), "")
            for tid, label in self.themes.THEMES.items():
                self.theme.addItem(tr(label), tid)
            row.addWidget(self.theme, 1)
            apply = QPushButton(tr("Apply"))
            apply.clicked.connect(self.apply_theme)
            row.addWidget(apply)
            v.addLayout(row)
        else:
            self.theme = None
        v.addWidget(QLabel(tr("Map doctor")))
        self.findings = QListWidget()
        v.addWidget(self.findings, 1)
        row = QHBoxLayout()
        self.fix_btn = QPushButton(tr("Fix selected"))
        self.fix_btn.clicked.connect(self.fix_selected)
        self.fix_all_btn = QPushButton(tr("Fix all"))
        self.fix_all_btn.clicked.connect(self.fix_all)
        row.addWidget(self.fix_btn)
        row.addWidget(self.fix_all_btn)
        row.addStretch(1)
        v.addLayout(row)
        if self.doctor is None:
            self.findings.addItem(tr("The map doctor is not installed."))
            self.fix_btn.setEnabled(False)
            self.fix_all_btn.setEnabled(False)
        return w

    # ---- step 3 --------------------------------------------------------------------------------------------
    def _page_export(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(tr("One map, every size it is needed in:")))
        chosen = QgsSettings().value(OUTPUTS_KEY, ["pdf", "png", "card"], type=list) or []
        self.out_boxes = {}
        for oid, label in intents.OUTPUTS.items():
            cb = QCheckBox(tr(label))
            cb.setChecked(oid in chosen)
            v.addWidget(cb)
            self.out_boxes[oid] = cb
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("Folder")))
        self.folder = QLineEdit(QgsSettings().value(DIR_KEY, os.path.expanduser("~"), type=str))
        row.addWidget(self.folder, 1)
        browse = QPushButton(tr("Browse…"))
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        v.addLayout(row)
        self.export_btn = QPushButton(tr("Export"))
        self.export_btn.clicked.connect(self.export)
        v.addWidget(self.export_btn)
        v.addStretch(1)
        return w

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, tr("Folder"), self.folder.text())
        if d:
            self.folder.setText(d)

    # ---- behaviour ------------------------------------------------------------------------------------------
    def go(self, index):
        index = max(0, min(2, index))
        if index > 0 and self.layout is None:
            index = 0
        self.stack.setCurrentIndex(index)
        for k, s in enumerate(self.steps):
            s.setStyleSheet(f"font-weight: {'bold' if k == index else 'normal'}; "
                            f"color: {ACCENT if k == index else '#6B7580'}; padding: 2px 10px;")
        self.back_btn.setEnabled(index > 0)
        self.next_btn.setEnabled(index < 2 and self.layout is not None)
        self.designer_btn.setEnabled(self.layout is not None)
        if index == 1:
            self.run_doctor()

    def choose(self, iid, advance=False):
        self.purpose = iid
        for k, b in self.cards.items():
            b.setChecked(k == iid)
        self.rebuild()
        if advance:
            self.go(1)

    def rebuild(self):
        if not self.purpose:
            return
        self.layout = auto_layout.build(self.project, self.purpose, title=self.title.text().strip() or None,
                                        add_to_manager=False)
        self.refresh_preview()
        self.go(self.stack.currentIndex())

    def refresh_preview(self):
        if self.layout is None:
            return
        page = self.layout.pageCollection().page(0).pageSize()
        dpi = max(10.0, min(200.0, PREVIEW_PX / max(page.width(), page.height()) * 25.4))
        img = QgsLayoutExporter(self.layout).renderPageToImage(0, QSize(), dpi)
        self.preview.setPixmap(QPixmap.fromImage(img).scaled(PREVIEW_PX, PREVIEW_PX, Qt.AspectRatioMode.KeepAspectRatio,
                                                             Qt.TransformationMode.SmoothTransformation))
        intent = intents.get(self.purpose)
        mp = self.layout.itemById("map")
        scale = f"1:{round(mp.scale()):,}" if mp is not None and mp.scale() > 0 else ""
        self.info.setText(tr("{what} · {w} × {h} mm · {scale}").format(
            what=tr(intent.label), w=round(page.width()), h=round(page.height()), scale=scale))

    def apply_theme(self):
        if self.theme is None or self.themes is None:
            return
        tid = self.theme.currentData()
        if tid:
            try:
                self.themes.apply_theme(auto_layout.drawn_layers(self.project), tid, self.purpose)
            except TypeError:
                self.themes.apply_theme(auto_layout.drawn_layers(self.project), tid)
            self.rebuild()

    def run_doctor(self):
        if self.doctor is None or self.layout is None:
            return
        self.findings.clear()
        try:
            found = self.doctor.check(self.layout, self.purpose)
        except Exception as e:  # noqa: BLE001 - shown to the user
            self.findings.addItem(tr("The map doctor could not check this map: {e}").format(e=e))
            return
        if not found:
            self.findings.addItem(tr("No problems found."))
        for f in found:
            it = QListWidgetItem(f"{'⚠' if getattr(f, 'severity', '') in ('error', 'warning') else 'ℹ'}  "
                                 f"{f.title} — {f.detail}")
            it.setData(Qt.ItemDataRole.UserRole, f)
            self.findings.addItem(it)
        self._found = found

    def fix_selected(self):
        it = self.findings.currentItem()
        f = it.data(Qt.ItemDataRole.UserRole) if it is not None else None
        if f is None or self.doctor is None or not getattr(f, "fixable", False):
            return
        msg = self.doctor.fix(self.layout, f)
        self.refresh_preview()
        self.run_doctor()
        if msg:
            self.info.setText(str(msg))

    def fix_all(self):
        if self.doctor is None or self.layout is None:
            return
        self.doctor.fix_all(self.layout, getattr(self, "_found", []))
        self.refresh_preview()
        self.run_doctor()

    def _register(self):
        """Put the layout in the project's layout manager (once) so it is saved with the project."""
        manager = self.project.layoutManager()
        if self.layout is not None and self.layout not in manager.layouts():
            self.layout.setName(auto_layout._unique_name(self.project, self.layout.name()))
            manager.addLayout(self.layout)

    def open_designer(self):
        if self.layout is None:
            return
        self._register()
        iface = getattr(self.plugin, "iface", None)
        if iface is not None:
            iface.openLayoutDesigner(self.layout)
        self.accept()

    def export(self):
        if self.layout is None:
            return
        chosen = [k for k, cb in self.out_boxes.items() if cb.isChecked()]
        folder = self.folder.text().strip()
        if not chosen or not folder:
            return
        s = QgsSettings()
        s.setValue(DIR_KEY, folder)
        s.setValue(OUTPUTS_KEY, chosen)
        self._register()
        done = outputs.export_all(self.layout, folder, chosen)
        QMessageBox.information(self, tr("Map Studio"), outputs.summary(done))


def open_studio(plugin):
    dlg = StudioDialog(plugin)
    dlg.exec()
    return dlg.layout
