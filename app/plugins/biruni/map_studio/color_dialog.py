"""Colour check window: the current map (or a print layout) side by side as seen with normal vision, the three
colour-vision deficiencies, in grayscale and as a rough CMYK print, plus the brand palette and the land-use
colour standards."""
from qgis.core import QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QMetaType, Qt
from qgis.PyQt.QtGui import QColor, QPainter, QPixmap
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QHBoxLayout,
                                 QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from ..i18n import tr
from . import palettes

PANEL_W, PANEL_H = 360, 260


class ColorPreviewDialog(QDialog):
    def __init__(self, plugin=None, layout=None, image=None, parent=None):
        super().__init__(parent)
        self.plugin, self.layout_ = plugin, layout
        self.setWindowTitle(tr("Check map colours"))
        v = QVBoxLayout(self)
        v.addWidget(QLabel(tr("How your map looks to people with colour-blindness, on a black-and-white copy "
                              "and in print. Colours that merge in a panel need a pattern, a label or more contrast.")))
        grid = QGridLayout()
        self.panels = []
        for i, title in enumerate(palettes.PROOFS):
            box = QVBoxLayout()
            cap = QLabel(tr(title))
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pic = QLabel()
            pic.setMinimumSize(PANEL_W // 2, PANEL_H // 2)
            pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.addWidget(pic, 1)
            box.addWidget(cap)
            w = QWidget()
            w.setLayout(box)
            grid.addWidget(w, i // 3, i % 3)
            self.panels.append(pic)
        v.addLayout(grid, 1)

        # standards and brand palette
        row = QHBoxLayout()
        self.standard = QComboBox()
        for sid, std in palettes.STANDARDS.items():
            mark = tr("verified") if std["status"] == "verified" else tr("common convention")
            self.standard.addItem(f"{tr(std['label'])} ({mark})", sid)
        apply_std = QPushButton(tr("Colour the selected layer by this standard"))
        apply_std.clicked.connect(self._apply_standard)
        apply_sem = QPushButton(tr("Colours by meaning"))
        apply_sem.setToolTip(tr("Residential yellow, commercial red, parks green, water blue… read from the values "
                                "of the layer's categories (Arabic or English)."))
        apply_sem.clicked.connect(self._apply_semantic)
        row.addWidget(self.standard, 1)
        row.addWidget(apply_std)
        row.addWidget(apply_sem)
        v.addLayout(row)

        brand = QHBoxLayout()
        logo = QPushButton(tr("Brand colours from a logo…"))
        logo.clicked.connect(self._pick_logo)
        self.swatches = QLabel()
        brand.addWidget(logo)
        brand.addWidget(self.swatches, 1)
        v.addLayout(brand)
        self._show_swatches(palettes.brand_palette())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        refresh = buttons.addButton(tr("Refresh"), QDialogButtonBox.ButtonRole.ActionRole)
        refresh.clicked.connect(self.refresh)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self.resize(3 * PANEL_W + 60, 2 * PANEL_H + 220)
        self.source_image = image
        self.refresh()

    # ---- rendering ----
    def _canvas(self):
        iface = getattr(self.plugin, "iface", None)
        return iface.mapCanvas() if iface is not None else None

    def map_image(self):
        """The picture to check: the given image, the layout's first page or the map canvas content."""
        if self.source_image is not None:
            return self.source_image
        if self.layout_ is not None:
            return palettes.render_layout_page(self.layout_)
        canvas = self._canvas()
        if canvas is not None:
            ms = canvas.mapSettings()
            return palettes.render_map(ms.layers(), ms.extent(), ms.destinationCrs(), 2 * PANEL_W, 2 * PANEL_H,
                                       ms.backgroundColor().name())
        project = QgsProject.instance()
        layers = [lyr for lyr in project.mapLayers().values() if lyr.isSpatial()]
        if not layers:
            return None
        ext = layers[0].extent()
        for lyr in layers[1:]:
            ext.combineExtentWith(lyr.extent())
        return palettes.render_map(layers, ext, layers[0].crs(), 2 * PANEL_W, 2 * PANEL_H)

    def refresh(self):
        self.images = []
        img = self.map_image()
        if img is None or img.isNull():
            for p in self.panels:
                p.setText(tr("Nothing to show yet: add layers to the map."))
            return
        for pic, (_title, im) in zip(self.panels, palettes.proofs(img)):
            self.images.append(im)
            pic.setPixmap(QPixmap.fromImage(im).scaled(PANEL_W, PANEL_H, Qt.AspectRatioMode.KeepAspectRatio,
                                                       Qt.TransformationMode.SmoothTransformation))

    # ---- actions ----
    def _layer(self):
        iface = getattr(self.plugin, "iface", None)
        lyr = iface.activeLayer() if iface is not None else None
        if not isinstance(lyr, QgsVectorLayer):
            QMessageBox.information(self, self.windowTitle(), tr("Select a vector layer in the Layers panel first."))
            return None, None
        field = None
        r = lyr.renderer()
        if hasattr(r, "classAttribute"):
            field = r.classAttribute()
        if not field or lyr.fields().indexOf(field) < 0:
            names = [f.name() for f in lyr.fields() if f.type() == QMetaType.Type.QString]
            field = next((n for n in names if any(w in n.lower() for w in ("use", "type", "class", "استعمال",
                                                                           "نوع", "تصنيف"))),
                         names[0] if names else None)
        if not field:
            QMessageBox.information(self, self.windowTitle(), tr("The layer has no text field to colour by."))
            return None, None
        return lyr, field

    def _report(self, res):
        msg = tr("{m} values coloured.").format(m=res["matched"])
        if res["unmatched"]:
            shown = ", ".join(str(u) for u in res["unmatched"][:12])
            msg += "\n" + tr("Not recognised (given neutral colours): {v}").format(v=shown)
        if res.get("status") == "approximate":
            msg += "\n\n" + tr("No official colour table is published for this standard; these are the colours "
                               "commonly used. Check them against the authority's requirements.")
        QMessageBox.information(self, self.windowTitle(), msg)

    def _apply_standard(self):
        lyr, field = self._layer()
        if lyr is not None:
            self._report(palettes.apply_standard(lyr, field, self.standard.currentData()))
            self.refresh()

    def _apply_semantic(self):
        lyr, field = self._layer()
        if lyr is not None:
            self._report(palettes.apply_semantic(lyr, field))
            self.refresh()

    def _pick_logo(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Brand colours from a logo…"), "",
                                              tr("Images (*.png *.jpg *.jpeg *.bmp *.svg)"))
        if not path:
            return
        try:
            cols = palettes.make_accessible(palettes.palette_from_image(path, 6))
        except ValueError as e:
            QMessageBox.warning(self, self.windowTitle(), str(e))
            return
        palettes.save_brand_palette(cols)
        self._show_swatches(cols)
        QMessageBox.information(self, self.windowTitle(),
                                tr("Saved. Choose the 'Brand colours' theme in Map Studio to use them."))

    def _show_swatches(self, cols):
        if not cols:
            self.swatches.setText(tr("No brand colours saved yet."))
            return
        pm = QPixmap(28 * len(cols), 22)
        pm.fill(QColor("#FFFFFF"))
        p = QPainter(pm)
        for i, c in enumerate(cols):
            p.fillRect(i * 28 + 2, 2, 24, 18, QColor(c))
        p.end()
        self.swatches.setPixmap(pm)
        self.swatches.setToolTip(" ".join(cols))


def open_color_preview(plugin, layout=None):
    dlg = ColorPreviewDialog(plugin, layout, parent=getattr(getattr(plugin, "iface", None), "mainWindow",
                                                            lambda: None)())
    dlg.show()
    plugin._color_preview = dlg  # keep it alive
    return dlg
