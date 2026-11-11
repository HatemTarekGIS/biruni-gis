"""Wizard of the study area starter pack: area → data → project, then the Processing tool runs it."""
import os
import tempfile

from qgis.core import (QgsFeature, QgsField, QgsFields, QgsGeometry, QgsMapLayerProxyModel,
                       QgsProcessingFeatureSourceDefinition, QgsSettings, QgsVectorLayer)
from qgis.PyQt.QtCore import QDate, QMetaType, Qt
from qgis.PyQt.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QDateEdit, QFileDialog, QFormLayout,
                                 QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton,
                                 QRadioButton, QSpinBox, QVBoxLayout, QWizard, QWizardPage)

from ..i18n import tr
from ..version import APP_NAME

KEY = "biruni/thesis-starter/"
ALG = "biruni:thesisstarter"


class StarterWizard(QWizard):
    def __init__(self, plugin):
        super().__init__(plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.sources = None  # OnlineSources by default (tests put a fake here)
        self._cands = []
        self._dialogs = []
        self.setWindowTitle(tr("Study Area Starter Pack"))
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setMinimumSize(640, 520)
        self.addPage(self._area_page())
        self.addPage(self._data_page())
        self.addPage(self._project_page())
        self.setButtonText(QWizard.WizardButton.FinishButton, tr("Make the project"))

    # ---- page 1: the study area ------------------------------------------------------------
    def _area_page(self):
        from qgis.gui import QgsExtentWidget, QgsMapLayerComboBox

        p = QWizardPage()
        p.setTitle(tr("Where is the study area?"))
        p.setSubTitle(tr("Type a place name, draw or take an extent, or use a polygon layer you already have."))
        v = QVBoxLayout(p)
        self.mode = QButtonGroup(p)
        self.by_place = QRadioButton(tr("A place name"))
        self.by_extent = QRadioButton(tr("An extent (draw it on the map or use the map view)"))
        self.by_layer = QRadioButton(tr("A polygon layer"))
        for k, b in enumerate((self.by_place, self.by_extent, self.by_layer)):
            self.mode.addButton(b, k)
        self.by_place.setChecked(True)
        v.addWidget(self.by_place)
        row = QHBoxLayout()
        self.place = QLineEdit()
        self.place.setPlaceholderText(tr("e.g. Al Olaya, Riyadh - or - Dokki, Giza"))
        self.place.returnPressed.connect(self.search_place)
        find = QPushButton(tr("Search"))
        find.clicked.connect(self.search_place)
        row.addWidget(self.place, 1)
        row.addWidget(find)
        v.addLayout(row)
        self.results = QListWidget()
        self.results.setMaximumHeight(120)
        self.results.currentRowChanged.connect(lambda _r: self.by_place.setChecked(True))
        v.addWidget(self.results)
        v.addWidget(self.by_extent)
        self.extent = QgsExtentWidget(p)
        canvas = self.iface.mapCanvas()
        if canvas is not None:
            self.extent.setMapCanvas(canvas)
            self.extent.setCurrentExtent(canvas.extent(), canvas.mapSettings().destinationCrs())
            self.extent.setOutputCrs(canvas.mapSettings().destinationCrs())
        self.extent.extentChanged.connect(lambda _e: self.by_extent.setChecked(True))
        v.addWidget(self.extent)
        v.addWidget(self.by_layer)
        row = QHBoxLayout()
        self.layer = QgsMapLayerComboBox()
        self.layer.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        self.layer.layerChanged.connect(lambda _l: self.by_layer.setChecked(True))
        self.selected_only = QCheckBox(tr("Selected features only"))
        row.addWidget(self.layer, 1)
        row.addWidget(self.selected_only)
        v.addLayout(row)
        if self.layer.currentLayer() is None:
            self.by_layer.setEnabled(False)
        return p

    def search_place(self):
        from .. import i18n
        from . import starter

        text = self.place.text().strip()
        if not text:
            return
        self.results.clear()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._cands = (self.sources or starter.OnlineSources()).geocode(text, None, i18n.lang())
        except Exception as e:  # noqa: BLE001
            self._cands = []
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME, tr("The place search did not answer: {e}").format(e=e))
            return
        QApplication.restoreOverrideCursor()
        for c in self._cands:
            self.results.addItem(("▭ " if "POLYGON" in c["wkt"].upper() else "• ") + c["name"])
        if not self._cands:
            self.results.addItem(tr("Nothing found - try another spelling or add the city or country."))
        else:
            first = next((k for k, c in enumerate(self._cands) if "POLYGON" in c["wkt"].upper()), 0)
            self.results.setCurrentRow(first)

    # ---- page 2: the data -----------------------------------------------------------------------
    def _data_page(self):
        from .starter import OSM_KEYS, OSM_TITLES

        s = QgsSettings()
        p = QWizardPage()
        p.setTitle(tr("Which data?"))
        p.setSubTitle(tr("Everything is free and is cut to the study area. Big areas take longer; OpenStreetMap "
                         "buildings and places are limited to 60 km²."))
        v = QVBoxLayout(p)
        self.boundaries = QCheckBox(tr("Administrative boundaries (country, regions, districts)"))
        self.boundaries.setChecked(s.value(KEY + "boundaries", True, type=bool))
        v.addWidget(self.boundaries)
        v.addWidget(QLabel("<b>" + tr("OpenStreetMap") + "</b>"))
        grid = QGridLayout()
        self.osm = {}
        for k, key in enumerate(OSM_KEYS):
            cb = QCheckBox(tr(OSM_TITLES[key]))
            cb.setChecked(s.value(KEY + "osm_" + key, True, type=bool))
            self.osm[key] = cb
            grid.addWidget(cb, k // 2, k % 2)
        v.addLayout(grid)
        self.terrain = QCheckBox(tr("Elevation (30 m) with slope, aspect, hillshade and contours"))
        self.terrain.setChecked(s.value(KEY + "terrain", True, type=bool))
        v.addWidget(self.terrain)
        self.landcover = QCheckBox(tr("Land cover (10 m) with the area of each class"))
        self.landcover.setChecked(s.value(KEY + "landcover", True, type=bool))
        v.addWidget(self.landcover)
        self.sentinel = QCheckBox(tr("Sentinel-2 satellite image: true colour, false colour and NDVI"))
        self.sentinel.setChecked(s.value(KEY + "sentinel", True, type=bool))
        v.addWidget(self.sentinel)
        row = QHBoxLayout()
        today = QDate.currentDate()
        self.s2_start = QDateEdit(today.addDays(-90))
        self.s2_end = QDateEdit(today)
        for d in (self.s2_start, self.s2_end):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
        self.cloud = QSpinBox()
        self.cloud.setRange(0, 100)
        self.cloud.setValue(20)
        self.cloud.setSuffix(" %")
        row.addSpacing(24)
        row.addWidget(QLabel(tr("From")))
        row.addWidget(self.s2_start)
        row.addWidget(QLabel(tr("to")))
        row.addWidget(self.s2_end)
        row.addWidget(QLabel(tr("clouds at most")))
        row.addWidget(self.cloud)
        row.addStretch(1)
        v.addLayout(row)
        self.population = QCheckBox(tr("Population 2020 (WorldPop, 100 m)"))
        self.population.setChecked(s.value(KEY + "population", True, type=bool))
        v.addWidget(self.population)
        self.location_map = QCheckBox(tr("Location map for chapter 1 (country → region → study area)"))
        self.location_map.setChecked(True)
        v.addWidget(self.location_map)
        v.addStretch(1)
        return p

    # ---- page 3: the project ----------------------------------------------------------------------
    def _project_page(self):
        from ..project import default_location

        p = QWizardPage()
        p.setTitle(tr("The project"))
        p.setSubTitle(tr("A new project folder is made; the current project is not changed."))
        form = QFormLayout(p)
        self.name = QLineEdit()
        self.name.setPlaceholderText(tr("Name of the study area"))
        form.addRow(tr("Name"), self.name)
        row = QHBoxLayout()
        self.folder = QLineEdit(QgsSettings().value(KEY + "folder", default_location()))
        browse = QPushButton("…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        form.addRow(tr("Create in"), row)
        note = QLabel(tr("The project gets a summary of the area, a data sources file with ready citations for the "
                         "references chapter, and a print layout of the location map."))
        note.setWordWrap(True)
        form.addRow(note)
        return p

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, tr("Create the project folder in"), self.folder.text())
        if d:
            self.folder.setText(d)

    # ---- parameters ----------------------------------------------------------------------------------------
    def _candidate_layer(self, cand):
        from . import starter

        area = starter.area_from_candidate(cand)
        path = os.path.join(tempfile.mkdtemp(prefix="biruni_place_"), "study_area.gpkg")
        fields = QgsFields()
        fields.append(QgsField("name", QMetaType.Type.QString))
        lyr = QgsVectorLayer("MultiPolygon?crs=EPSG:4326", "place", "memory")
        lyr.dataProvider().addAttributes(fields.toList())
        lyr.updateFields()
        f = QgsFeature(lyr.fields())
        g = QgsGeometry(area.geometry)
        g.convertToMultiType()
        f.setGeometry(g)
        f.setAttributes([cand["name"]])
        lyr.dataProvider().addFeatures([f])
        starter.write_table(path, "study_area", lyr)
        return f"{path}|layername=study_area", area.name

    def parameters(self):
        from .starter import OSM_KEYS

        params = {}
        name = self.name.text().strip()
        if self.by_place.isChecked():
            row = self.results.currentRow()
            if 0 <= row < len(self._cands):
                params["AREA"], default = self._candidate_layer(self._cands[row])
                name = name or (self.place.text().split(",")[0].strip() or default)
            elif self.place.text().strip():
                params["PLACE"] = self.place.text().strip()
                name = name or params["PLACE"].split(",")[0].strip()
        elif self.by_extent.isChecked():
            r = self.extent.outputExtent()
            crs = self.extent.outputCrs()
            params["EXTENT"] = f"{r.xMinimum()},{r.xMaximum()},{r.yMinimum()},{r.yMaximum()} [{crs.authid()}]"
        elif self.by_layer.isChecked() and self.layer.currentLayer() is not None:
            lyr = self.layer.currentLayer()
            params["AREA"] = QgsProcessingFeatureSourceDefinition(lyr.id(), self.selected_only.isChecked()) \
                if self.selected_only.isChecked() else lyr.id()
            name = name or lyr.name()
        params.update({
            "NAME": name, "FOLDER": self.folder.text().strip(), "BOUNDARIES": self.boundaries.isChecked(),
            "OSM": [k for k, key in enumerate(OSM_KEYS) if self.osm[key].isChecked()],
            "TERRAIN": self.terrain.isChecked(), "LANDCOVER": self.landcover.isChecked(),
            "SENTINEL": self.sentinel.isChecked(), "S2_START": self.s2_start.date(), "S2_END": self.s2_end.date(),
            "CLOUD": self.cloud.value(), "POPULATION": self.population.isChecked(),
            "LOCATION_MAP": self.location_map.isChecked()})
        return params

    def validateCurrentPage(self):  # noqa: N802
        if self.currentId() == 0:
            if self.by_place.isChecked() and not self.place.text().strip():
                QMessageBox.information(self, APP_NAME, tr("Type the name of a place, or choose another way."))
                return False
            if self.by_place.isChecked() and self.place.text().strip() and not self._cands:
                self.search_place()
                if not self._cands:
                    return False
            if not self.name.text().strip():
                self.name.setText(self.place.text().split(",")[0].strip() if self.by_place.isChecked() else
                                  (self.layer.currentLayer().name() if self.by_layer.isChecked() and
                                   self.layer.currentLayer() else tr("Study area")))
        if self.currentId() == 2 and not self.folder.text().strip():
            return False
        return super().validateCurrentPage()

    def _remember(self):
        s = QgsSettings()
        s.setValue(KEY + "folder", self.folder.text().strip())
        for k in ("boundaries", "terrain", "landcover", "sentinel", "population"):
            s.setValue(KEY + k, getattr(self, k).isChecked())
        for key, cb in self.osm.items():
            s.setValue(KEY + "osm_" + key, cb.isChecked())

    # ---- run -------------------------------------------------------------------------------------------------
    def accept(self):
        params = self.parameters()
        self._remember()
        super().accept()
        run_with_dialog(self.plugin, params)


def run_with_dialog(plugin, params):
    """Open the Processing dialog of the tool with the wizard's choices and start it at once (progress, log and
    Cancel are the Processing ones). When it ends, offer to open the new project."""
    import processing
    from qgis.PyQt.QtCore import QTimer

    dlg = processing.createAlgorithmDialog(ALG, params)
    if dlg is None:
        return None
    keep = getattr(plugin, "_starter_dialogs", None)
    if keep is None:
        keep = plugin._starter_dialogs = []
    keep.append(dlg)

    def done(ok, results):
        qgz = (results or {}).get("PROJECT")
        if not ok or not qgz or not os.path.exists(qgz):
            return
        lines = (results.get("REPORT") or "").splitlines()
        failed = [x for x in lines if x.startswith("FAILED")]
        msg = tr("The project of the study area is ready:\n{p}").format(p=os.path.dirname(qgz))
        if failed:
            msg += "\n\n" + tr("Not downloaded:") + "\n" + "\n".join(failed)
        msg += "\n\n" + tr("Open it now?")
        if QMessageBox.question(plugin.iface.mainWindow(), APP_NAME, msg) == QMessageBox.StandardButton.Yes:
            if plugin.iface.newProject(True):
                plugin.iface.addProject(qgz)

    if hasattr(dlg, "algorithmFinished"):
        dlg.algorithmFinished.connect(done)
    dlg.show()
    if hasattr(dlg, "runAlgorithm"):
        QTimer.singleShot(200, dlg.runAlgorithm)
    return dlg


def show(plugin):
    """Entry point of the ribbon button."""
    wiz = StarterWizard(plugin)
    plugin._starter_wizard = wiz
    wiz.show()
    return wiz
