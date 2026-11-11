"""Dialog of the methodology chapter: review the recorded steps (include, reorder, rename), preview the text and the
workflow diagram, copy the text or export the chapter."""
import html
import os
import tempfile

from qgis.core import QgsProject
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                                 QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea,
                                 QSplitter, QTabWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from ..i18n import lang as ui_lang
from ..i18n import tr
from . import methodology as m

ROLE = Qt.ItemDataRole.UserRole


class MethodologyDialog(QDialog):
    def __init__(self, parent=None, lang=None, steps=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Methodology chapter"))
        self.resize(1100, 720)
        self._tmp = tempfile.mkdtemp(prefix="biruni_methodology_")
        self.steps = steps if steps is not None else m.methodology_steps()

        top = QHBoxLayout()
        top.addWidget(QLabel(tr("Language of the chapter")))
        self.lang_combo = QComboBox()
        self.lang_combo.addItem(tr("Arabic"), "ar")
        self.lang_combo.addItem(tr("English"), "en")
        code = m._lang(lang or ui_lang())
        self.lang_combo.setCurrentIndex(self.lang_combo.findData(code))
        top.addWidget(self.lang_combo)
        top.addStretch(1)
        refresh = QPushButton(tr("Refresh"))
        refresh.setToolTip(tr("Read the project again (new tools run, layers added)"))
        top.addWidget(refresh)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(tr("Untick a step to leave it out. Drag or use the arrows to reorder; double-click to rename."))
        hint.setWordWrap(True)
        lv.addWidget(hint)
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                  | QAbstractItemView.EditTrigger.EditKeyPressed)
        lv.addWidget(self.list, 1)
        arrows = QHBoxLayout()
        up, down = QToolButton(), QToolButton()
        up.setArrowType(Qt.ArrowType.UpArrow)
        down.setArrowType(Qt.ArrowType.DownArrow)
        up.setToolTip(tr("Move up"))
        down.setToolTip(tr("Move down"))
        arrows.addWidget(up)
        arrows.addWidget(down)
        arrows.addStretch(1)
        lv.addLayout(arrows)

        self.tabs = QTabWidget()
        self.preview = QTextBrowser()
        self.tabs.addTab(self.preview, tr("Text"))
        self.diagram = QLabel()
        self.diagram.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.diagram)
        self.tabs.addTab(scroll, tr("Workflow diagram"))

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.tabs)
        split.setStretchFactor(1, 2)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = buttons.addButton(tr("Copy as text"), QDialogButtonBox.ButtonRole.ActionRole)
        save_fig = buttons.addButton(tr("Save diagram…"), QDialogButtonBox.ButtonRole.ActionRole)
        export = buttons.addButton(tr("Export chapter…"), QDialogButtonBox.ButtonRole.ActionRole)

        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)
        self.status = QLabel()
        lay.addWidget(self.status)
        lay.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.update_preview)
        self._filling = False

        buttons.rejected.connect(self.reject)
        copy.clicked.connect(self.copy_text)
        save_fig.clicked.connect(self.save_diagram)
        export.clicked.connect(self.export_dialog)
        refresh.clicked.connect(self.refresh)
        up.clicked.connect(lambda: self.move_step(-1))
        down.clicked.connect(lambda: self.move_step(1))
        self.lang_combo.currentIndexChanged.connect(self._on_lang)
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.model().rowsMoved.connect(lambda *a: self._schedule())
        self.fill()

    # ---- state ----------------------------------------------------------------
    def lang(self):
        return self.lang_combo.currentData() or "en"

    def fill(self):
        self._filling = True
        self.list.clear()
        for i, s in enumerate(self.steps):
            it = QListWidgetItem(m.step_label(s, self.lang()))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable
                        | Qt.ItemFlag.ItemIsDragEnabled)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            it.setCheckState(Qt.CheckState.Checked if s.get("include", True) else Qt.CheckState.Unchecked)
            it.setData(ROLE, i)
            it.setToolTip(self._kind_name(s))
            self.list.addItem(it)
        self._filling = False
        self.status.setText(tr("{n} steps found in this project.").format(n=len(self.steps)) if self.steps else
                            tr("Nothing recorded yet: add data and run tools, then press Refresh."))
        self.update_preview()

    @staticmethod
    def _kind_name(s):
        return {"data": tr("Data source"), "crs": tr("Coordinate system"), "tool": tr("Tool"),
                "edit": tr("Manual editing"), "layout": tr("Map layout")}.get(s.get("kind"), "")

    def current_steps(self):
        """The steps as the user arranged them: order, include flag and renamed labels."""
        out = []
        for row in range(self.list.count()):
            it = self.list.item(row)
            s = dict(self.steps[it.data(ROLE)])
            s["include"] = it.checkState() == Qt.CheckState.Checked
            base = m.step_label(dict(s, label=None), self.lang())
            text = it.text().strip()
            s["label"] = text if text and text != base else None
            out.append(s)
        return out

    def _sync_back(self):
        """Keep renames/include flags when the list is rebuilt (language switch)."""
        cur = self.current_steps()
        self.steps = cur

    # ---- events ------------------------------------------------------------------
    def _schedule(self):
        if not self._filling:
            self._timer.start()

    def _on_item_changed(self, _it):
        self._schedule()
        # the timer is for typing; check-box toggles are applied at once
        if not self._filling:
            self.update_preview()

    def _on_lang(self):
        self._sync_back()
        self.fill()

    def move_step(self, delta):
        row = self.list.currentRow()
        new = row + delta
        if row < 0 or not 0 <= new < self.list.count():
            return
        self._filling = True
        it = self.list.takeItem(row)
        self.list.insertItem(new, it)
        self.list.setCurrentRow(new)
        self._filling = False
        self.update_preview()

    def refresh(self):
        old = {s["id"]: s for s in self.current_steps()}
        fresh = m.methodology_steps()
        for s in fresh:
            o = old.get(s["id"])
            if o is not None and o.get("title") == s.get("title"):
                s["include"] = o["include"]
                s["label"] = o.get("label")
        self.steps = fresh
        self.fill()

    # ---- output --------------------------------------------------------------------
    def document(self):
        return m.methodology_document(self.lang(), self.current_steps())

    def update_preview(self):
        doc = self.document()
        e = html.escape
        rtl = doc["rtl"]
        body = [f"<h2>{e(doc['title'])}</h2>"] + [f"<p>{e(p)}</p>" for p in doc["paragraphs"]]
        body.append(f"<h3>{e(doc['steps_title'])}</h3><ol>" + "".join(f"<li>{e(s)}</li>" for s in doc["steps"])
                    + "</ol>")
        if doc["sources"]:
            body.append(f"<p><i>{e(doc['table_caption'])}</i></p><table border='1' cellspacing='0' cellpadding='3'>"
                        "<tr>" + "".join(f"<th>{e(h)}</th>" for h in doc["sources_header"]) + "</tr>"
                        + "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>"
                                  for r in doc["sources"]) + "</table>")
        body.append(f"<h3>{e(doc['refs_title'])}</h3><ol>"
                    + "".join(f"<li dir='ltr'>{e(r)}</li>" for r in doc["references"]) + "</ol>")
        self.preview.setLayoutDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
        self.preview.setHtml(f"<div dir='{'rtl' if rtl else 'ltr'}'>{''.join(body)}</div>")
        try:
            png = m.workflow_image(os.path.join(self._tmp, "preview.png"), self.lang(), self.current_steps(),
                                   width=760)
            pix = QPixmap(png)
            pix.setDevicePixelRatio(2.0)
            self.diagram.setPixmap(pix)
        except Exception as ex:  # the text stays usable even if drawing fails
            self.diagram.setText(str(ex))

    def copy_text(self):
        QApplication.clipboard().setText(m.plain_text(self.document()))
        self.status.setText(tr("The chapter was copied - paste it into your document."))

    def _start_dir(self):
        try:
            from ..project import project_folder

            folder = project_folder()
        except Exception:
            folder = None
        if folder:
            return os.path.join(folder, "exports")
        f = QgsProject.instance().fileName()
        return os.path.dirname(f) if f else os.path.expanduser("~")

    def save_diagram(self):
        path, _ = QFileDialog.getSaveFileName(self, tr("Save diagram…"),
                                              os.path.join(self._start_dir(), tr("Workflow") + ".png"),
                                              "PNG (*.png);;SVG (*.svg)")
        if not path:
            return None
        m.workflow_image(path, self.lang(), self.current_steps())
        self.status.setText(tr("Saved: {path}").format(path=path))
        return path

    def export_dialog(self):
        if m.docx_available():
            filters = tr("Word document (*.docx)") + ";;" + tr("Web page, opens in Word (*.html)") + ";;" + \
                tr("Markdown (*.md)")
            name = tr("Methodology") + ".docx"
        else:
            filters = tr("Web page, opens in Word (*.html)") + ";;" + tr("Markdown (*.md)")
            name = tr("Methodology") + ".html"
        path, _ = QFileDialog.getSaveFileName(self, tr("Export chapter…"), os.path.join(self._start_dir(), name),
                                              filters)
        if not path:
            return None
        return self.export_to(path)

    def export_to(self, path):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            files = m.export_methodology(path, self.lang(), self.current_steps())
        except OSError as ex:
            QApplication.restoreOverrideCursor()
            self.status.setText(tr("Could not save: {error}").format(error=ex))
            return None
        QApplication.restoreOverrideCursor()
        self.status.setText(tr("Saved: {path}").format(path=files[0]))
        return files


def show_methodology(plugin=None):
    """Open the dialog (command entry point)."""
    parent = plugin.iface.mainWindow() if plugin is not None else None
    dlg = MethodologyDialog(parent)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.show()
    return dlg
