"""The thesis figures window: the project's figures with their numbers, a new figure from the current map, moving
and renumbering, the university style (built-in, own or shared JSON) and the export of every figure with the list
of figures."""
import os

from qgis.core import QgsProject, QgsSettings
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
                                 QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLineEdit, QMessageBox,
                                 QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout)

from ..i18n import tr
from . import figures as tf

DIR_KEY = "biruni/thesis/figures_folder"
COLS = ["Chapter", "Order", "Number", "Caption", "English caption", "Layout"]


class FiguresDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(getattr(plugin, "mw", None))
        self.plugin = plugin
        self.project = QgsProject.instance()
        self._filling = False
        self.setWindowTitle(tr("Thesis figures"))
        self.resize(1000, 640)
        root = QVBoxLayout(self)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels([tr(c) for c in COLS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._edited)
        self.table.doubleClicked.connect(lambda _i: self.open_designer())
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        for label, slot in ((tr("Move up"), lambda: self.move(-1)), (tr("Move down"), lambda: self.move(1)),
                            (tr("Delete figure"), self.delete), (tr("Renumber"), self.renumber),
                            (tr("Open in layout designer"), self.open_designer)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        root.addLayout(row)

        # a new figure
        new = QGroupBox(tr("New figure from the current map"))
        form = QFormLayout(new)
        self.chapter = QSpinBox()
        self.chapter.setRange(1, 99)
        self.chapter.setValue(1)
        self.caption = QLineEdit()
        self.caption.setPlaceholderText("توزيع السكان في منطقة الدراسة")
        self.caption_en = QLineEdit()
        self.caption_en.setPlaceholderText("Population distribution in the study area")
        self.source = QLineEdit()
        self.source.setPlaceholderText(tr("e.g. field survey 2026 (layer sources are added by themselves)"))
        form.addRow(tr("Chapter"), self.chapter)
        form.addRow(tr("Caption (Arabic)"), self.caption)
        form.addRow(tr("Caption (English)"), self.caption_en)
        form.addRow(tr("Source"), self.source)
        add = QPushButton(tr("Add figure"))
        add.clicked.connect(self.add)
        form.addRow("", add)

        # the style
        sty = QGroupBox(tr("University style"))
        sform = QFormLayout(sty)
        self.style_box = QComboBox()
        self.style_box.currentIndexChanged.connect(self._style_chosen)
        sform.addRow(tr("Style"), self.style_box)
        self.page = QComboBox()
        self.page.addItems(list(tf.PAGES))
        self.orientation = QComboBox()
        for key, text in (("portrait", "Portrait"), ("landscape", "Landscape"), ("auto", "From the data")):
            self.orientation.addItem(tr(text), key)
        self.language = QComboBox()
        for key, text in (("ar", "Arabic"), ("en", "English"), ("both", "Arabic and English")):
            self.language.addItem(tr(text), key)
        self.numbering = QComboBox()
        for key, text in (("chapter", "Per chapter (3-2)"), ("continuous", "Continuous (1, 2, 3…)")):
            self.numbering.addItem(tr(text), key)
        self.grid = QComboBox()
        for key, text in (("degrees", "Degrees and minutes"), ("metres", "Projected metres"), ("none", "No grid")):
            self.grid.addItem(tr(text), key)
        self.inset = QCheckBox(tr("Location map (inset)"))
        self.font_ar = QLineEdit()
        self.font_latin = QLineEdit()
        self.pattern_ar = QLineEdit()
        self.pattern_en = QLineEdit()
        for label, w in ((tr("Page"), self.page), (tr("Orientation"), self.orientation),
                         (tr("Language"), self.language), (tr("Numbering"), self.numbering),
                         (tr("Coordinate grid"), self.grid), ("", self.inset), (tr("Arabic font"), self.font_ar),
                         (tr("Latin font"), self.font_latin), (tr("Arabic caption"), self.pattern_ar),
                         (tr("English caption"), self.pattern_en)):
            sform.addRow(label, w)
        srow = QHBoxLayout()
        for label, slot in ((tr("Apply to all figures"), self.apply_all), (tr("Save as my style…"), self.save_style),
                            (tr("Import style…"), self.import_style), (tr("Export style…"), self.export_style)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            srow.addWidget(b)
        sform.addRow(srow)

        mid = QHBoxLayout()
        mid.addWidget(new, 1)
        mid.addWidget(sty, 1)
        root.addLayout(mid)

        out = QHBoxLayout()
        self.dpi = QSpinBox()
        self.dpi.setRange(72, 1200)
        self.dpi.setValue(300)
        self.dpi.setSuffix(" dpi")
        self.fmt_png, self.fmt_jpg, self.fmt_pdf = QCheckBox("PNG"), QCheckBox("JPG"), QCheckBox("PDF")
        self.fmt_png.setChecked(True)
        self.fmt_pdf.setChecked(True)
        for w in (self.dpi, self.fmt_png, self.fmt_jpg, self.fmt_pdf):
            out.addWidget(w)
        b = QPushButton(tr("Export all figures…"))
        b.clicked.connect(self.export_all)
        out.addWidget(b)
        b = QPushButton(tr("List of figures…"))
        b.clicked.connect(self.export_list)
        out.addWidget(b)
        out.addStretch(1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.accept)
        out.addWidget(close)
        root.addLayout(out)

        self._fill_styles()
        tf.watch(self.project)
        self.refresh()

    # ---- the list ------------------------------------------------------------------------------------------------
    def refresh(self, select=None):
        self._filling = True
        figs = tf.figures(self.project)
        self.table.setRowCount(len(figs))
        for r, f in enumerate(figs):
            values = [str(f["chapter"]), str(f["order"]), f["number"], f["caption"], f["caption_en"], f["layout"]]
            for c, v in enumerate(values):
                it = QTableWidgetItem(v)
                if c not in (0, 3, 4):
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if c == 3:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                it.setData(Qt.ItemDataRole.UserRole, f["layout"])
                self.table.setItem(r, c, it)
            if select is not None and f["layout"] == select:
                self.table.selectRow(r)
        self._filling = False
        if figs:
            self.chapter.setValue(max(f["chapter"] for f in figs))

    def _selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        name = self.table.item(rows[0].row(), 5).text()
        return self.project.layoutManager().layoutByName(name)

    def _edited(self, item):
        if self._filling:
            return
        lay = self.project.layoutManager().layoutByName(item.data(Qt.ItemDataRole.UserRole) or "")
        if lay is None:
            return
        col = item.column()
        try:
            if col == 0:
                tf.set_figure(lay, chapter=max(1, int(item.text())), project=self.project)
            elif col == 3:
                tf.set_figure(lay, caption=item.text(), project=self.project)
            elif col == 4:
                tf.set_figure(lay, caption_en=item.text(), project=self.project)
        except ValueError:
            pass
        self.refresh(lay.name())

    # ---- actions --------------------------------------------------------------------------------------------------
    def add(self):
        if not self.caption.text().strip() and not self.caption_en.text().strip():
            QMessageBox.information(self, tr("Thesis figures"), tr("Write the figure's caption first."))
            return
        extent = None
        iface = getattr(self.plugin, "iface", None)
        if iface is not None:
            extent = iface.mapCanvas().extent()
        lay = tf.add_figure(self.project, self.chapter.value(), self.caption.text().strip(),
                            self.caption_en.text().strip(), self.current_style(), extent=extent,
                            source=self.source.text().strip())
        self.caption.clear()
        self.caption_en.clear()
        self.refresh(lay.name())

    def move(self, step):
        lay = self._selected()
        if lay is None:
            return
        fig = next((f for f in tf.figures(self.project) if f["layout"] == lay.name()), None)
        if fig is None:
            return
        tf.move_figure(lay, fig["chapter"], max(1, fig["order"] + step), self.project)
        self.refresh(lay.name())

    def delete(self):
        lay = self._selected()
        if lay is None:
            return
        ask = QMessageBox.question(self, tr("Thesis figures"),
                                   tr("Delete the figure «{name}» and its print layout?").format(name=lay.name()))
        if ask == QMessageBox.StandardButton.Yes:
            tf.delete_figure(lay, project=self.project)
            self.refresh()

    def renumber(self):
        tf.renumber(self.project)
        self.refresh()

    def open_designer(self):
        lay = self._selected()
        iface = getattr(self.plugin, "iface", None)
        if lay is not None and iface is not None:
            iface.openLayoutDesigner(lay)

    # ---- styles ---------------------------------------------------------------------------------------------------
    def _fill_styles(self, select=None):
        self.style_box.blockSignals(True)
        self.style_box.clear()
        for s in tf.all_styles():
            self.style_box.addItem(tr(s["name"]) if s["id"] in tf.BUILTIN else s["name"], s["id"])
        k = self.style_box.findData(select) if select else 0
        self.style_box.setCurrentIndex(max(0, k))
        self.style_box.blockSignals(False)
        self._style_chosen()

    def _style_chosen(self, *_a):
        s = tf.get_style(self.style_box.currentData())
        self.page.setCurrentText(s["page"] if isinstance(s["page"], str) else "A4")
        for box, key in ((self.orientation, "orientation"), (self.language, "language"),
                         (self.numbering, "numbering"), (self.grid, "grid")):
            box.setCurrentIndex(max(0, box.findData(s[key])))
        self.inset.setChecked(bool(s["inset"]))
        self.font_ar.setText(s["font_ar"])
        self.font_latin.setText(s["font_latin"])
        self.pattern_ar.setText(s["caption_ar"])
        self.pattern_en.setText(s["caption_en"])

    def current_style(self):
        s = tf.get_style(self.style_box.currentData())
        s.update(page=self.page.currentText(), orientation=self.orientation.currentData(),
                 language=self.language.currentData(), numbering=self.numbering.currentData(),
                 grid=self.grid.currentData(), inset=self.inset.isChecked(), font_ar=self.font_ar.text().strip(),
                 font_latin=self.font_latin.text().strip(), caption_ar=self.pattern_ar.text(),
                 caption_en=self.pattern_en.text())
        return tf.normalise_style(s)

    def apply_all(self):
        tf.apply_style_all(self.current_style(), self.project)
        self.refresh()

    def save_style(self):
        name, ok = QInputDialog.getText(self, tr("Save as my style…"), tr("Name of the style (e.g. your university)"))
        if not ok or not name.strip():
            return
        s = self.current_style()
        s["name"] = name.strip()
        s["id"] = "user_" + "".join(ch if ch.isalnum() else "_" for ch in name.strip().lower())[:40]
        saved = tf.save_user_style(s)
        self._fill_styles(saved["id"])

    def import_style(self):
        path, _f = QFileDialog.getOpenFileName(self, tr("Import style…"), self._folder(), "JSON (*.json)")
        if not path:
            return
        try:
            s = tf.import_style(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, tr("Thesis figures"), str(e))
            return
        self._fill_styles(s["id"])

    def export_style(self):
        s = self.current_style()
        path, _f = QFileDialog.getSaveFileName(self, tr("Export style…"),
                                               os.path.join(self._folder(), f"{s['id']}.json"), "JSON (*.json)")
        if path:
            tf.export_style(s, path)

    # ---- exports --------------------------------------------------------------------------------------------------
    def _folder(self):
        return QgsSettings().value(DIR_KEY, os.path.expanduser("~")) or os.path.expanduser("~")

    def export_all(self):
        if not tf.figures(self.project):
            QMessageBox.information(self, tr("Thesis figures"), tr("The project has no thesis figures yet."))
            return
        folder = QFileDialog.getExistingDirectory(self, tr("Export all figures…"), self._folder())
        if not folder:
            return
        QgsSettings().setValue(DIR_KEY, folder)
        formats = [f for f, box in (("png", self.fmt_png), ("jpg", self.fmt_jpg), ("pdf", self.fmt_pdf))
                   if box.isChecked()] or ["png"]
        res = tf.export_all(folder, self.dpi.value(), formats, self.project)
        text = tr("{n} files written").format(n=len(res["files"]) + len(res["list"]))
        if res["failed"]:
            text += "\n" + tr("Not exported: {what}").format(what=", ".join(res["failed"]))
        QMessageBox.information(self, tr("Thesis figures"), text)

    def export_list(self):
        path, _f = QFileDialog.getSaveFileName(self, tr("List of figures…"),
                                               os.path.join(self._folder(), "List_of_figures.docx"),
                                               "Word (*.docx);;HTML (*.html)")
        if not path:
            return
        written = tf.list_of_figures(path, self.project)
        QMessageBox.information(self, tr("Thesis figures"),
                                tr("{n} files written").format(n=len(written)) + "\n" + "\n".join(written))


def open_dialog(plugin):
    dlg = FiguresDialog(plugin)
    dlg.exec()
    return dlg
