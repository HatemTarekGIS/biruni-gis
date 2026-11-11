"""Dialog: the thesis defence presentation - the details of the title slide, the language and the look, the slide list
(reorder, leave out) with a preview, and the export to a folder."""
import datetime as dt
import json
import os

from qgis.core import QgsProject
from qgis.PyQt.QtCore import Qt, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon, QPixmap
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
                                 QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                                 QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressDialog,
                                 QPushButton, QSplitter, QVBoxLayout, QWidget)

from ..i18n import tr
from ..version import APP_NAME
from . import defense

SETTINGS_KEY = "defense"  # project entry ("Biruni", "thesis/defense"), JSON of the dialog's inputs


def _project_title(project):
    return project.title() or os.path.splitext(os.path.basename(project.fileName()))[0]


def _default_folder(project):
    try:
        from ..project import project_folder

        folder = project_folder(project)
    except Exception:  # noqa: BLE001
        folder = None
    if not folder and project.fileName():
        folder = os.path.dirname(project.fileName())
    folder = folder or os.path.join(os.path.expanduser("~"), "Documents")
    return os.path.join(folder, "exports", "defense")


class DefenseDialog(QDialog):
    """Make the presentation for the defence day: PowerPoint, an HTML page for phones, and a PDF."""

    def __init__(self, plugin, project=None):
        parent = plugin.iface.mainWindow() if plugin is not None and getattr(plugin, "iface", None) else None
        super().__init__(parent)
        self.plugin = plugin
        self.project = project or QgsProject.instance()
        self.setWindowTitle(tr("Defence presentation"))
        self.setMinimumSize(900, 600)
        self.slides = []
        self.accent = "#0E7C74"
        saved = self._load()

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 12)
        intro = QLabel(tr("Makes the presentation for the defence day from this project's maps and results: a "
                          "PowerPoint file, one HTML page the supervisor and the examiners can open on their phones "
                          "(with an interactive map of the study area), and a PDF for printing."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        split = QSplitter(Qt.Orientation.Horizontal)
        v.addWidget(split, 1)

        # left: the inputs
        left = QWidget()
        form = QFormLayout(left)
        form.setContentsMargins(0, 0, 8, 0)
        self.title = QLineEdit(saved.get("title") or _project_title(self.project))
        self.title_en = QLineEdit(saved.get("title_en", ""))
        self.title_en.setPlaceholderText(tr("Optional - shown under the title when both languages are used"))
        self.students = QPlainTextEdit("\n".join(saved.get("students") or []))
        self.students.setPlaceholderText(tr("One name per line"))
        self.students.setFixedHeight(64)
        self.supervisor = QLineEdit(saved.get("supervisor", ""))
        self.university = QLineEdit(saved.get("university", ""))
        self.faculty = QLineEdit(saved.get("faculty", ""))
        self.date = QLineEdit(saved.get("date") or dt.date.today().strftime("%Y-%m-%d"))
        self.lang = QComboBox()
        for code, text in (("ar", tr("Arabic (right to left)")), ("en", tr("English")),
                           ("both", tr("Arabic and English"))):
            self.lang.addItem(text, code)
        self.lang.setCurrentIndex(max(0, self.lang.findData(saved.get("lang", "ar"))))
        self.theme = QComboBox()
        self.theme.addItem(tr("Light"), "light")
        self.theme.addItem(tr("Dark"), "dark")
        self.theme.setCurrentIndex(max(0, self.theme.findData(saved.get("theme", "light"))))
        self.accent = saved.get("accent") or self.accent
        self.accent_btn = QPushButton(tr("Accent colour…"))
        self.accent_btn.clicked.connect(self._pick_accent)
        self._paint_accent()
        self.url = QLineEdit(saved.get("url", ""))
        self.url.setPlaceholderText(tr("Optional - where the HTML page will be published (QR code on the last slide)"))
        self.pptx = QCheckBox(tr("PowerPoint (.pptx)"))
        self.pptx.setChecked(saved.get("pptx", True))
        self.pdf = QCheckBox(tr("PDF for printing"))
        self.pdf.setChecked(saved.get("pdf", True))
        out_row = QHBoxLayout()
        out_row.addWidget(self.pptx)
        out_row.addWidget(self.pdf)
        out_row.addStretch(1)
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(saved.get("folder") or _default_folder(self.project))
        browse = QPushButton(tr("Browse…"))
        browse.clicked.connect(self._browse)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)

        form.addRow(tr("Title"), self.title)
        form.addRow(tr("Title in English"), self.title_en)
        form.addRow(tr("Student(s)"), self.students)
        form.addRow(tr("Supervisor"), self.supervisor)
        form.addRow(tr("University"), self.university)
        form.addRow(tr("Faculty / department"), self.faculty)
        form.addRow(tr("Date"), self.date)
        form.addRow(tr("Language"), self.lang)
        form.addRow(tr("Theme"), self.theme)
        form.addRow("", self.accent_btn)
        form.addRow(tr("Web address"), self.url)
        form.addRow(tr("Also make"), out_row)
        form.addRow(tr("Folder"), folder_row)
        split.addWidget(left)

        # right: the slide list and a preview of the selected slide
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(8, 0, 0, 0)
        rv.addWidget(QLabel(tr("Slides - untick to leave a slide out, drag to reorder")))
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.currentRowChanged.connect(self._preview)
        rv.addWidget(self.list, 1)
        btns = QHBoxLayout()
        up = QPushButton(tr("Up"))
        down = QPushButton(tr("Down"))
        remove = QPushButton(tr("Remove"))
        reset = QPushButton(tr("Reset list"))
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        remove.clicked.connect(self._remove)
        reset.clicked.connect(self.reload_slides)
        for b in (up, down, remove, reset):
            btns.addWidget(b)
        btns.addStretch(1)
        rv.addLayout(btns)
        self.preview = QLabel()
        self.preview.setFixedSize(384, 216)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("border:1px solid palette(mid)")
        rv.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignHCenter)
        split.addWidget(right)
        split.setSizes([460, 420])

        bb = QDialogButtonBox()
        self.ok = bb.addButton(tr("Make presentation"), QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        bb.accepted.connect(self._run)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        self.lang.currentIndexChanged.connect(self._relabel)
        self.theme.currentIndexChanged.connect(lambda _i: self._preview(self.list.currentRow()))
        self.reload_slides()

    # ---- inputs ----
    def options(self):
        return {
            "title": self.title.text().strip(),
            "title_en": self.title_en.text().strip(),
            "students": [s.strip() for s in self.students.toPlainText().splitlines() if s.strip()],
            "supervisor": self.supervisor.text().strip(),
            "university": self.university.text().strip(),
            "faculty": self.faculty.text().strip(),
            "date": self.date.text().strip(),
            "lang": self.lang.currentData(),
            "theme": self.theme.currentData(),
            "accent": self.accent,
            "url": self.url.text().strip(),
        }

    def _load(self):
        text, ok = self.project.readEntry("Biruni", "thesis/" + SETTINGS_KEY, "")
        if not ok or not text:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self):
        data = self.options()
        data.update(folder=self.folder.text().strip(), pptx=self.pptx.isChecked(), pdf=self.pdf.isChecked())
        self.project.writeEntry("Biruni", "thesis/" + SETTINGS_KEY, json.dumps(data, ensure_ascii=False))

    def _pick_accent(self):
        c = QColorDialog.getColor(QColor(self.accent), self, tr("Accent colour"))
        if c.isValid():
            self.accent = c.name()
            self._paint_accent()
            self._preview(self.list.currentRow())

    def _paint_accent(self):
        pm = QPixmap(16, 16)
        pm.fill(QColor(self.accent))
        self.accent_btn.setIcon(QIcon(pm))

    def _browse(self):
        p = QFileDialog.getExistingDirectory(self, tr("Folder"), self.folder.text())
        if p:
            self.folder.setText(os.path.normpath(p))

    # ---- the slide list ----
    def reload_slides(self):
        self.slides = defense.build_slides(self.project, self.options())
        self.list.clear()
        lang = self.lang.currentData()
        for i, s in enumerate(self.slides):
            text = defense.slide_label(s, lang)
            if s.get("html_only"):
                text += "  " + tr("(HTML page only)")
            it = QListWidgetItem(text)
            it.setData(Qt.ItemDataRole.UserRole, i)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            it.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _relabel(self, _i=None):
        lang = self.lang.currentData()
        for r in range(self.list.count()):
            it = self.list.item(r)
            s = self.slides[it.data(Qt.ItemDataRole.UserRole)]
            text = defense.slide_label(s, lang)
            if s.get("html_only"):
                text += "  " + tr("(HTML page only)")
            it.setText(text)
        self._preview(self.list.currentRow())

    def _move(self, step):
        r = self.list.currentRow()
        t = r + step
        if r < 0 or t < 0 or t >= self.list.count():
            return
        it = self.list.takeItem(r)
        self.list.insertItem(t, it)
        self.list.setCurrentRow(t)

    def _remove(self):
        r = self.list.currentRow()
        if r >= 0:
            self.list.takeItem(r)

    def chosen_slides(self):
        out = []
        for r in range(self.list.count()):
            it = self.list.item(r)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(dict(self.slides[it.data(Qt.ItemDataRole.UserRole)]))
        return out

    def _preview(self, row):
        """A quick sketch of the slide (pictures are drawn only in the export)."""
        if row is None or row < 0 or row >= self.list.count():
            self.preview.clear()
            return
        try:
            opts = defense._norm_opts(self.options())
            s = dict(self.slides[self.list.item(row).data(Qt.ItemDataRole.UserRole)])
            s.pop("image", None)
            chosen = self.chosen_slides()
            comp = defense.Composer(opts)
            prims = comp.compose(s, row + 1, max(1, len(chosen)), defense.outline_items(chosen))
            img = defense.slide_image(prims, defense.palette(opts["theme"], opts["accent"]), 384)
            self.preview.setPixmap(QPixmap.fromImage(img))
        except Exception:  # noqa: BLE001 - a preview must never break the dialog
            self.preview.clear()

    # ---- export ----
    def _run(self):
        slides = self.chosen_slides()
        folder = self.folder.text().strip()
        if not slides:
            QMessageBox.information(self, APP_NAME, tr("Choose at least one slide."))
            return
        if not folder:
            QMessageBox.information(self, APP_NAME, tr("Choose a folder."))
            return
        self._save()
        outputs = ["html"] + (["pptx"] if self.pptx.isChecked() else []) + (["pdf"] if self.pdf.isChecked() else [])
        prog = QProgressDialog(tr("Making the presentation…"), "", 0, 100, self)
        prog.setCancelButton(None)
        prog.setWindowModality(Qt.WindowModality.WindowModal)
        prog.setMinimumDuration(0)

        def feedback(i, n, text):
            prog.setMaximum(max(1, n))
            prog.setValue(i)
            if text:
                prog.setLabelText(tr("Making the presentation…") + "\n" + text)
            QApplication.processEvents()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = defense.export_presentation(folder, slides, self.options(), self.project, outputs, feedback)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            prog.close()
            QMessageBox.warning(self, APP_NAME, tr("The presentation was not made: {error}").format(error=e))
            return
        QApplication.restoreOverrideCursor()
        prog.close()
        act = getattr(self.plugin, "activity", None)
        if act is not None:
            try:
                act.log("thesis", "defense", self.title.text(), "", result.get("slides", 0), result.get("html", ""))
            except Exception:  # noqa: BLE001
                pass
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(defense.summary(result))
        open_btn = box.addButton(tr("Open folder"), QMessageBox.ButtonRole.AcceptRole)
        show_btn = box.addButton(tr("Open the HTML page"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        elif box.clickedButton() is show_btn and result.get("html"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(result["html"]))


def open_dialog(plugin):
    DefenseDialog(plugin).exec()
