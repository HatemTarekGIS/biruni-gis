"""Small windows for the CRS doctor, Google Earth tours and KML, and publishing a website folder."""
import html
import os

from qgis.core import Qgis, QgsProject, QgsSettings, QgsVectorLayer
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                                 QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .i18n import tr
from .version import APP_NAME

SEVERITY_COLOURS = {"error": "#d64545", "warning": "#d99a1e", "info": "#3b82c4"}


def _layers(p):
    """The vector layers chosen in the Layers panel, else the visible ones."""
    chosen = [l for l in p.iface.layerTreeView().selectedLayers() if isinstance(l, QgsVectorLayer)]
    if chosen:
        return chosen
    root = QgsProject.instance().layerTreeRoot()
    return [n.layer() for n in root.findLayers()
            if n.isVisible() and isinstance(n.layer(), QgsVectorLayer) and n.layer().isValid()]


# ---- CRS doctor ------------------------------------------------------------------------------
class CrsDoctorDialog(QDialog):
    def __init__(self, plugin):
        super().__init__(plugin.mw)
        self.plugin = plugin
        self.setWindowTitle(tr("Why don't my layers line up?"))
        self.resize(640, 520)
        v = QVBoxLayout(self)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        v.addWidget(self.scroll)
        self.status = QLabel()
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        row = QHBoxLayout()
        again = QPushButton(tr("Check again"))
        again.clicked.connect(self.refresh)
        row.addWidget(again)
        row.addStretch(1)
        close = QPushButton(tr("Close"))
        close.clicked.connect(self.accept)
        row.addWidget(close)
        v.addLayout(row)
        self.refresh()

    def refresh(self):
        from . import crs_doctor

        findings = crs_doctor.analyse()
        body = QWidget()
        lay = QVBoxLayout(body)
        if not findings:
            lay.addWidget(QLabel(tr("No problems found: every layer has a coordinate system and they line up.")))
        for f in findings:
            colour = SEVERITY_COLOURS.get(f.severity, "#888")
            box = QLabel(f"<b style='color:{colour}'>{html.escape(f.title)}</b>"
                         f"{' — ' + html.escape(f.layer_name) if f.layer_name else ''}<br>"
                         f"<span>{html.escape(f.detail)}</span>")
            box.setWordWrap(True)
            box.setTextFormat(Qt.TextFormat.RichText)
            lay.addWidget(box)
            if f.fixes:
                row = QHBoxLayout()
                for fix in f.fixes:
                    b = QPushButton(fix["label"])
                    b.clicked.connect(lambda _=False, fx=fix: self._apply(fx))
                    row.addWidget(b)
                row.addStretch(1)
                lay.addLayout(row)
        lay.addStretch(1)
        self.scroll.setWidget(body)

    def _apply(self, fix):
        from . import crs_doctor

        try:
            msg = crs_doctor.apply_fix(fix, iface=self.plugin.iface)
        except Exception as e:  # noqa: BLE001 - shown to the user
            msg = tr("The fix could not be applied: {e}").format(e=e)
        self.status.setText(str(msg))
        self.plugin.iface.mapCanvas().refreshAllLayers()
        self.refresh()


def crs_doctor(p):
    CrsDoctorDialog(p).exec()


# ---- Google Earth -----------------------------------------------------------------------------
def earth_tour(p):
    from .share import kml_tour

    layers = _layers(p)
    if not layers:
        p.iface.messageBar().pushWarning(APP_NAME, tr("Choose the layers for the tour in the Layers panel."))
        return
    s = QgsSettings()
    start = s.value("biruni/earth-tour/dir", os.path.expanduser("~"))
    path, _ = QFileDialog.getSaveFileName(p.mw, tr("Google Earth tour"), os.path.join(start, "tour.kmz"),
                                          tr("Google Earth (*.kmz *.kml)"))
    if not path:
        return
    s.setValue("biruni/earth-tour/dir", os.path.dirname(path))
    lines = [l for l in layers if l.geometryType() == Qgis.GeometryType.Line]
    others = [l for l in layers if l not in lines[:1]]
    path, stops = kml_tour.export_tour(others or layers, path, QgsProject.instance().title() or tr("Tour"),
                                       route=lines[0] if lines else None)
    p.iface.messageBar().pushSuccess(APP_NAME, tr("Tour saved with {n} stops: {p}").format(n=stops, p=path))


def import_kml(p):
    from .share import kml_tour

    s = QgsSettings()
    path, _ = QFileDialog.getOpenFileName(p.mw, tr("Open KML / KMZ (with colours)"),
                                          s.value("biruni/earth-tour/dir", os.path.expanduser("~")),
                                          tr("Google Earth (*.kmz *.kml)"))
    if not path:
        return
    s.setValue("biruni/earth-tour/dir", os.path.dirname(path))
    layers = kml_tour.import_kml(path)
    p.iface.messageBar().pushSuccess(APP_NAME, tr("{n} layer(s) opened from {f}.").format(
        n=len(layers), f=os.path.basename(path)))


# ---- website ----------------------------------------------------------------------------------
def publish_site(p):
    from .share import pages_publish

    layers = _layers(p)
    if not layers:
        p.iface.messageBar().pushWarning(APP_NAME, tr("Choose the layers to publish in the Layers panel."))
        return
    s = QgsSettings()
    folder = QFileDialog.getExistingDirectory(p.mw, tr("Folder for the website (an empty folder)"),
                                              s.value("biruni/publish-site/dir", os.path.expanduser("~")))
    if not folder:
        return
    s.setValue("biruni/publish-site/dir", folder)
    if os.listdir(folder) and QMessageBox.question(
            p.mw, APP_NAME, tr("The folder is not empty; files with the same names will be replaced. Continue?")
    ) != QMessageBox.StandardButton.Yes:
        return
    from .share import offline_maps

    packs = offline_maps.basemap_packs()
    pack = None
    if packs and QMessageBox.question(
            p.mw, APP_NAME, tr("Put the offline basemap of the map into the site, cut to the area of the data? "
                               "Then the site needs no internet at all.")) == QMessageBox.StandardButton.Yes:
        pack = packs[0]
    result = pages_publish.publish_site(layers, folder, QgsProject.instance().title() or tr("Map"), basemap_pack=pack)
    text = tr("Website written to {f}. Read README.md for the three steps to put it online.").format(f=folder)
    if result.get("warnings"):
        text += "\n" + "\n".join(result["warnings"])
    if result.get("mode") == "tiles":
        ask = text + "\n\n" + tr("Open it in the browser now (from a preview on this computer)?")
        if QMessageBox.question(p.mw, APP_NAME, ask) == QMessageBox.StandardButton.Yes:
            from .share.offline_maps_ui import preview_ui

            preview_ui(p, folder)
        return
    QMessageBox.information(p.mw, APP_NAME, text)
