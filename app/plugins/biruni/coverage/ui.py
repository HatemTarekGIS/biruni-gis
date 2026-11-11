"""The window of the parcel coverage doctor: examine a parcel layer as one fabric, then clean or simplify it in
its edits."""
from qgis.core import Qgis, QgsFillSymbol, QgsLineSymbol, QgsProject, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                                 QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout)

from ..i18n import tr
from ..version import APP_NAME
from . import core



def _polygon_layers():
    return [l for l in QgsProject.instance().mapLayers().values()
            if isinstance(l, QgsVectorLayer) and l.isValid() and l.geometryType() == QgsWkbTypes.GeometryType.PolygonGeometry]


def _metres(value, decimals=2, top=1000.0):
    s = QDoubleSpinBox()
    s.setRange(0, top)
    s.setDecimals(decimals)
    s.setValue(value)
    s.setSuffix(" " + tr("m"))
    return s


class CoverageDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.report = None
        self.added = []
        self.setWindowTitle(tr("Parcel Coverage Doctor"))
        self.setMinimumWidth(500)
        v = QVBoxLayout(self)
        intro = QLabel(tr("Checks a parcel layer as one fabric: the narrow gaps between parcels, the overlaps, the "
                          "shared sides drawn twice a few millimetres apart (the cause of slivers that come back), and "
                          "the slivers. «Clean» mends them all at once in the layer's edits: one Undo takes it back, "
                          "nothing is saved until you save."))
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.layer = QComboBox()
        for lyr in _polygon_layers():
            self.layer.addItem(lyr.name(), lyr.id())
        act = plugin.iface.activeLayer()
        if act is not None and self.layer.findData(act.id()) >= 0:
            self.layer.setCurrentIndex(self.layer.findData(act.id()))
        form.addRow(tr("Parcel layer"), self.layer)
        self.gap = _metres(0.5)
        self.gap.setToolTip(tr("A hole between parcels narrower than this is a gap to fill; a wider one (a courtyard, "
                               "a lake) is left."))
        form.addRow(tr("Largest gap width"), self.gap)
        self.sliver = _metres(0.2)
        form.addRow(tr("Sliver width"), self.sliver)
        self.only = QCheckBox(tr("Only the selected parcels"))
        form.addRow("", self.only)
        v.addLayout(form)
        row = QHBoxLayout()
        self.go = QPushButton(tr("Examine"))
        self.go.clicked.connect(lambda: self.examine())
        row.addWidget(self.go)
        row.addStretch(1)
        v.addLayout(row)

        fix = QGroupBox(tr("Mend"))
        ff = QFormLayout(fix)
        self.snap = _metres(0.0, 3, 10.0)
        self.snap.setSpecialValueText(tr("chosen from the layer's extent"))
        self.snap.setToolTip(tr("Vertices closer than this become one. Leave it at the start for a distance chosen "
                                "from the size of the layer."))
        ff.addRow(tr("Snapping distance"), self.snap)
        self.merge = QComboBox()
        for key, label in (("longest_border", tr("the neighbour with the longest shared side")),
                           ("max_area", tr("the largest neighbour")), ("min_area", tr("the smallest neighbour")),
                           ("min_index", tr("the first parcel"))):
            self.merge.addItem(label, key)
        ff.addRow(tr("An overlap goes to"), self.merge)
        self.clean_b = QPushButton(tr("Clean"))
        self.clean_b.clicked.connect(lambda: self.clean())
        ff.addRow("", self.clean_b)
        self.tol = _metres(0.1, 3, 100.0)
        self.keep = QCheckBox(tr("Keep the outer boundary as it is"))
        self.keep.setChecked(True)
        srow = QHBoxLayout()
        srow.addWidget(self.tol)
        srow.addWidget(self.keep)
        ff.addRow(tr("Simplify by"), srow)
        self.simp_b = QPushButton(tr("Simplify Keeping Shared Sides"))
        self.simp_b.clicked.connect(lambda: self.simplify())
        ff.addRow("", self.simp_b)
        v.addWidget(fix)

        self.out = QLabel("")
        self.out.setWordWrap(True)
        v.addWidget(self.out)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def current(self):
        return QgsProject.instance().mapLayer(self.layer.currentData() or "")

    def _only(self, lyr):
        if self.only.isChecked():
            ids = lyr.selectedFeatureIds()
            return ids or []
        return None

    def _drop_added(self):
        p = QgsProject.instance()
        for lid in self.added:
            if p.mapLayer(lid) is not None:
                p.removeMapLayer(lid)
        self.added = []

    def _fail(self, e):
        msg = str(e)
        if msg == "shapely":
            msg = tr("The geometry library of this installation is too old for the coverage tools.")
        elif msg == "not editable":
            msg = tr("The layer cannot be edited.")
        elif msg == "not polygons":
            msg = tr("Choose a polygon layer.")
        self.out.setText(msg)

    def examine(self):
        lyr = self.current()
        if lyr is None:
            self.out.setText(tr("Choose a polygon layer."))
            return None
        try:
            rep = core.examine(lyr, self.gap.value(), self.sliver.value(), self._only(lyr))
        except core.CoverageError as e:
            self._fail(e)
            return None
        self.report = rep
        self._drop_added()
        if rep.clean:
            text = tr("«{l}» is a clean coverage: {n} parcels, no gap, no overlap, every shared side one.").format(
                l=lyr.name(), n=rep.features)
        else:
            text = tr("{n} parcels: {g} gaps, {o} overlaps, {e} parcels with unmatched sides, {s} slivers, {i} invalid "
                      "shapes. They are on the map, each with what it is.").format(
                n=rep.features, g=rep.count("gap"), o=rep.count("overlap"), e=rep.count("edge"),
                s=rep.count("sliver"), i=rep.count("invalid"))
            for ml in core.problem_layers(rep, lyr):
                if ml.geometryType() == QgsWkbTypes.GeometryType.PolygonGeometry:
                    ml.renderer().setSymbol(QgsFillSymbol.createSimple(
                        {"color": "230,57,70,110", "outline_color": "230,57,70,255", "outline_width": "0.6"}))
                else:
                    ml.renderer().setSymbol(QgsLineSymbol.createSimple({"color": "255,140,0,255", "width": "1.2"}))
                QgsProject.instance().addMapLayer(ml)
                self.added.append(ml.id())
        if rep.curved:
            text += " " + tr("{c} parcels with arcs were checked on their arc's points; «Clean» leaves them as they "
                             "are.").format(c=len(rep.curved))
        self.out.setText(text)
        return rep

    def _done(self, res, verb):
        lyr = self.current()
        parts = [verb.format(n=res.changed, l=lyr.name() if lyr else "")]
        if res.curved:
            parts.append(tr("{c} parcels with arcs left as they are.").format(c=res.curved))
        if res.invalid:
            parts.append(tr("{c} invalid shapes left out: fix them first.").format(c=res.invalid))
        if res.split:
            parts.append(tr("{c} parcels would have become two pieces in a single-part layer: left.").format(c=res.split))
        if res.emptied:
            parts.append(tr("{c} parcels would have been left with nothing: left.").format(c=res.emptied))
        text = " ".join(parts)
        self.out.setText(text)
        self.plugin.iface.messageBar().pushMessage(APP_NAME, text, Qgis.MessageLevel.Success, 10)
        return res

    def clean(self):
        lyr = self.current()
        if lyr is None:
            self.out.setText(tr("Choose a polygon layer."))
            return None
        snap = self.snap.value() or None
        try:
            res = core.clean(lyr, self.gap.value(), snap, self._only(lyr), self.merge.currentData())
        except core.CoverageError as e:
            self._fail(e)
            return None
        self._drop_added()
        return self._done(res, tr("{n} parcels of «{l}» mended, in its edits: Undo takes it back, save to keep it."))

    def simplify(self):
        lyr = self.current()
        if lyr is None:
            self.out.setText(tr("Choose a polygon layer."))
            return None
        try:
            res = core.simplify(lyr, self.tol.value(), self.keep.isChecked(), self._only(lyr))
        except core.CoverageError as e:
            self._fail(e)
            return None
        return self._done(res, tr("{n} parcels of «{l}» simplified, their shared sides still one: Undo takes it back."))


def open_ui(plugin):
    if not _polygon_layers():
        plugin.iface.messageBar().pushMessage(APP_NAME, tr("Add a polygon layer to the map first."),
                                              Qgis.MessageLevel.Warning, 8)
        return None
    dlg = CoverageDialog(plugin)
    dlg.show()
    plugin._coverage_dialog = dlg
    return dlg
