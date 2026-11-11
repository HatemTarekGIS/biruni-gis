"""Small windows for the map-output tools: one generic "pick what the tool needs" form, then the call.

Map products (land use, topographic, subdivision plan, before/after, statistical atlas), place icons, symbols
by scale, bilingual labels, label importance, the hidden-labels report and the automatic caption."""
import os

from qgis.core import QgsMapLayerProxyModel, QgsVectorLayer
from qgis.gui import QgsFieldComboBox, QgsMapLayerComboBox
from qgis.PyQt.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                                 QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

from ..i18n import lang, tr
from ..version import APP_NAME

# kinds: "vector", "polygon", "line", "raster", "field:<layer key>", "fields:<layer key>" (comma list),
# "optfield:<layer key>", "save:<filter>"
FILTERS = {"vector": QgsMapLayerProxyModel.Filter.VectorLayer, "polygon": QgsMapLayerProxyModel.Filter.PolygonLayer,
           "line": QgsMapLayerProxyModel.Filter.LineLayer, "raster": QgsMapLayerProxyModel.Filter.RasterLayer,
           "optvector": QgsMapLayerProxyModel.Filter.VectorLayer, "optline": QgsMapLayerProxyModel.Filter.LineLayer}


class PickDialog(QDialog):
    def __init__(self, parent, title, specs):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.widgets = {}
        v = QVBoxLayout(self)
        form = QFormLayout()
        v.addLayout(form)
        active = getattr(parent, "_biruni_active_layer", None)
        for key, label, kind in specs:
            base, _, ref = kind.partition(":")
            if base in FILTERS:
                w = QgsMapLayerComboBox()
                w.setFilters(FILTERS[base])
                w.setAllowEmptyLayer(base.startswith("opt"))
                if active is not None and not base.startswith("opt"):
                    w.setLayer(active)
            elif base in ("field", "optfield"):
                w = QgsFieldComboBox()
                w.setAllowEmptyFieldName(base == "optfield")
                src = self.widgets[ref]
                w.setLayer(src.currentLayer())
                src.layerChanged.connect(w.setLayer)
            elif base == "fields":
                w = QLineEdit()
                w.setPlaceholderText(tr("Field names separated by commas"))
            elif base == "save":
                w = QWidget()
                h = QHBoxLayout(w)
                h.setContentsMargins(0, 0, 0, 0)
                edit = QLineEdit()
                btn = QPushButton("…")
                btn.clicked.connect(lambda _=False, e=edit, f=ref: e.setText(
                    QFileDialog.getSaveFileName(self, title, e.text() or os.path.expanduser("~"), f)[0] or e.text()))
                h.addWidget(edit, 1)
                h.addWidget(btn)
                w.edit = edit
            else:
                w = QLineEdit()
            self.widgets[key] = w
            form.addRow(label, w)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def values(self):
        out = {}
        for key, w in self.widgets.items():
            if isinstance(w, QgsMapLayerComboBox):
                out[key] = w.currentLayer()
            elif isinstance(w, QgsFieldComboBox):
                out[key] = w.currentField() or None
            elif hasattr(w, "edit"):
                out[key] = w.edit.text().strip()
            else:
                txt = w.text().strip()
                out[key] = [t.strip() for t in txt.split(",") if t.strip()] if "," in txt or txt else txt
        return out


def _ask(p, title, specs):
    p.mw._biruni_active_layer = p.iface.activeLayer()
    dlg = PickDialog(p.mw, title, specs)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return dlg.values()


def _done(p, text, ok=True):
    bar = p.iface.messageBar()
    (bar.pushSuccess if ok else bar.pushWarning)(APP_NAME, text)


def _run(p, fn):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - shown to the user
        _done(p, tr("It could not be done: {e}").format(e=e), ok=False)
        return None


def _open_layout(p, layout):
    if layout is not None:
        p.iface.openLayoutDesigner(layout)


# ---- map products ----------------------------------------------------------------------------------------------
def land_use(p):
    from . import products

    v = _ask(p, tr("Land use map"), [("layer", tr("Land use polygons"), "polygon"),
                                       ("field", tr("Use field"), "field:layer")])
    if v and v["layer"] is not None and v["field"]:
        _open_layout(p, _run(p, lambda: products.land_use_map(v["layer"], v["field"], lang=lang())))


def topographic(p):
    from . import products

    v = _ask(p, tr("Topographic map"), [("dem", tr("Elevation (DEM)"), "raster")])
    if v and v["dem"] is not None:
        _open_layout(p, _run(p, lambda: products.topographic_map(v["dem"], lang=lang())))


def subdivision(p):
    from . import products

    v = _ask(p, tr("Subdivision plan"), [("parcels", tr("Parcels"), "polygon"),
                                          ("number", tr("Parcel number field"), "optfield:parcels"),
                                          ("streets", tr("Streets (optional)"), "optvector"),
                                          ("width", tr("Street width field"), "optfield:streets"),
                                          ("name", tr("Street name field"), "optfield:streets")])
    if v and v["parcels"] is not None:
        _open_layout(p, _run(p, lambda: products.subdivision_plan(
            v["parcels"], number_field=v["number"], streets=v["streets"], width_field=v["width"],
            name_field=v["name"], lang=lang())))


def swipe(p):
    from . import products

    v = _ask(p, tr("Before / after page"), [("before", tr("Before"), "raster"), ("after", tr("After"), "raster"),
                                            ("path", tr("Save the page as"), "save:HTML (*.html)")])
    if v and v["before"] is not None and v["after"] is not None and v["path"]:
        path = _run(p, lambda: products.swipe_page(v["before"], v["after"], v["path"]))
        if path:
            _done(p, tr("Page saved: {p}").format(p=v["path"]))


def atlas(p):
    from . import products

    v = _ask(p, tr("Statistical atlas"), [("districts", tr("Districts"), "polygon"),
                                          ("name", tr("Name field"), "field:districts"),
                                          ("fields", tr("Fields to show"), "fields"),
                                          ("path", tr("Save the PDF as"), "save:PDF (*.pdf)")])
    if v and v["districts"] is not None and v["name"] and v["fields"] and v["path"]:
        fields = v["fields"] if isinstance(v["fields"], list) else [v["fields"]]
        if _run(p, lambda: products.statistical_atlas(v["districts"], v["name"], fields, v["path"],
                                                     lang=lang())) is not None:
            _done(p, tr("Atlas saved: {p}").format(p=v["path"]))


PRODUCT_CALLS = {"land_use": land_use, "topographic": topographic, "subdivision": subdivision, "swipe": swipe,
                 "atlas": atlas}


def products_menu(p, menu):
    from .products import PRODUCTS

    for pid, label in PRODUCTS.items():
        if pid in PRODUCT_CALLS:
            menu.addAction(tr(label)).triggered.connect(lambda _=False, f=PRODUCT_CALLS[pid]: f(p))


# ---- symbols and labels -----------------------------------------------------------------------------------------
def place_icons(p):
    from . import symbols

    v = _ask(p, tr("Icons from names"), [("layer", tr("Points"), "vector"), ("field", tr("Name or type field"),
                                                                             "field:layer")])
    if v and v["layer"] is not None and v["field"]:
        _run(p, lambda: symbols.apply_icons(v["layer"], v["field"]))
        v["layer"].triggerRepaint()


def symbols_by_scale(p):
    from . import symbols

    lyr = p.iface.activeLayer()
    if not isinstance(lyr, QgsVectorLayer):
        _done(p, tr("Choose a vector layer in the Layers panel first."), ok=False)
        return
    _run(p, lambda: symbols.scale_dependent(lyr))
    lyr.triggerRepaint()
    _done(p, tr("Simple dots when zoomed out, full symbols when zoomed in."))


def install_symbols(p):
    from . import symbols

    n = _run(p, symbols.install_style)
    _done(p, tr("Biruni symbols added to the style library."))
    return n


def bilingual_labels(p):
    from . import smart_labels

    v = _ask(p, tr("Labels in two languages"), [("layer", tr("Layer"), "vector"),
                                                ("ar", tr("Arabic name field"), "field:layer"),
                                                ("en", tr("English name field"), "field:layer")])
    if v and v["layer"] is not None and v["ar"] and v["en"]:
        _run(p, lambda: smart_labels.bilingual(v["layer"], v["ar"], v["en"]))
        v["layer"].triggerRepaint()


def label_priorities(p):
    from . import smart_labels

    v = _ask(p, tr("Label importance"), [("layer", tr("Layer"), "vector"),
                                         ("field", tr("Class or rank field"), "field:layer")])
    if v and v["layer"] is not None and v["field"]:
        _run(p, lambda: smart_labels.priorities(v["layer"], v["field"]))
        v["layer"].triggerRepaint()


def hidden_labels(p):
    from . import smart_labels

    report = _run(p, lambda: smart_labels.hidden_labels_report(p.iface.mapCanvas().mapSettings()))
    if report is not None:
        QMessageBox.information(p.mw, tr("Hidden labels"), smart_labels.report_text(report))


def caption(p):
    from . import captions

    v = _ask(p, tr("Map caption"), [("layer", tr("Layer"), "vector"), ("field", tr("Value field"), "field:layer"),
                                    ("name", tr("Name field"), "optfield:layer")])
    if not v or v["layer"] is None or not v["field"]:
        return
    text = _run(p, lambda: captions.caption(v["layer"], v["field"], v["name"], lang="ar" if lang() == "ar" else "en"))
    if not text:
        return
    dlg = QDialog(p.mw)
    dlg.setWindowTitle(tr("Map caption"))
    lay = QVBoxLayout(dlg)
    box = QPlainTextEdit(text)
    lay.addWidget(box)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    copy = bb.addButton(tr("Copy"), QDialogButtonBox.ButtonRole.ActionRole)
    copy.clicked.connect(lambda: QApplication.clipboard().setText(box.toPlainText()))
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    dlg.resize(520, 260)
    dlg.exec()
