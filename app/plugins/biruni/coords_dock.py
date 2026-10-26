"""Coordinates panel: convert, go to, pick from the map, keep as points."""
from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsPointXY, QgsProject, QgsSettings
from qgis.gui import QgsMapToolEmitPoint, QgsProjectionSelectionWidget, QgsVertexMarker
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QGuiApplication
from qgis.PyQt.QtWidgets import (QDockWidget, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QToolButton,
                                 QVBoxLayout, QWidget)

from . import coords, icons
from .i18n import tr
from .widgets import card, hint, panel_qss

KEY = "biruni/coords/"


class CoordinatesDock(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("Coordinates"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setObjectName("BiruniCoords")
        self.position = None
        self.marker = None
        self.layer = None
        self.tool = None
        self._prev_tool = None
        self._build()

    def _build(self):
        body = QWidget()
        body.setObjectName("BiruniCoordsBody")
        v = QVBoxLayout(body)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(8)
        t = self.plugin.theme.tokens()

        box, lay = card(v)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("30.0444, 31.2357  ·  30°02'40\"N 31°14'08\"E  ·  36N 330125 3324100")
        self.input.returnPressed.connect(self.convert)
        self.input.textChanged.connect(lambda *_: self.convert(quiet=True))
        self.pick = QToolButton()
        self.pick.setObjectName("Flat")
        self.pick.setIcon(icons.icon("crosshair", t["icon"]))
        self.pick.setToolTip(tr("Pick a point on the map"))
        self.pick.setCheckable(True)
        self.pick.toggled.connect(self._toggle_pick)
        row.addWidget(self.input, 1)
        row.addWidget(self.pick)
        lay.addLayout(row)
        lay.addWidget(hint(tr("Type or paste a coordinate in any format: degrees, degrees-minutes-seconds, UTM, "
                              "MGRS or a map link. For plain X Y values in another system, choose it here:")))
        self.crs = QgsProjectionSelectionWidget()
        saved = QgsSettings().value(KEY + "crs", "")
        start = QgsCoordinateReferenceSystem(saved) if saved else QgsProject.instance().crs()
        self.crs.setCrs(start if start.isValid() else coords.WGS84)
        self.crs.crsChanged.connect(lambda *_: self.convert(quiet=True))
        lay.addWidget(self.crs)
        self.status = QLabel()
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        out, olay = card(v)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(8)
        self.grid.setVerticalSpacing(4)
        olay.addLayout(self.grid)
        self.rows = []

        btns = QHBoxLayout()
        go = QPushButton(icons.icon("focus-2", t["icon"]), tr("Go to"))
        go.setObjectName("Primary")
        go.clicked.connect(self.go_to)
        keep = QPushButton(icons.icon("map-pin-plus", t["icon"]), tr("Add as point"))
        keep.clicked.connect(self.add_point)
        btns.addWidget(go)
        btns.addWidget(keep)
        btns.addStretch(1)
        v.addLayout(btns)
        v.addStretch(1)
        self.setWidget(body)
        self.setMinimumWidth(320)
        self.refresh_theme()
        self._show([])

    def refresh_theme(self):
        self.widget().setStyleSheet(panel_qss(self.plugin.theme.tokens(), "BiruniCoordsBody"))

    # ---- conversion --------------------------------------------------------------
    def convert(self, quiet=False):
        text = self.input.text()
        pos = coords.parse_any(text, self.crs.crs()) if text.strip() else None
        if pos is None or not (abs(pos[1]) <= 90 and abs(pos[0]) <= 180):
            self.position = None
            self._show([])
            if text.strip() and not quiet:
                self.status.setText(tr("This coordinate was not understood."))
            elif text.strip():
                self.status.setText("…")
            else:
                self.status.setText("")
            return None
        QgsSettings().setValue(KEY + "crs", self.crs.crs().authid())
        self.position = pos
        extra = [QgsProject.instance().crs()]
        if self.crs.crs().authid() != QgsProject.instance().crs().authid():
            extra.append(self.crs.crs())
        self._show(coords.formats(pos[0], pos[1], extra))
        self.status.setText("")
        return pos

    def _show(self, rows):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        t = self.plugin.theme.tokens()
        if not rows:
            lab = QLabel(tr("The result appears here in every format."))
            lab.setObjectName("Hint")
            self.grid.addWidget(lab, 0, 0)
            return
        for r, (label, text) in enumerate(rows):
            k = QLabel(tr(label))
            k.setObjectName("Key")
            val = QLabel(text if label != "Map link" else f"<a href='{text}'>OpenStreetMap</a>")
            val.setObjectName("Value")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            val.setOpenExternalLinks(True)
            val.setWordWrap(True)
            cp = QToolButton()
            cp.setObjectName("Flat")
            cp.setIcon(icons.icon("copy", t["icon"]))
            cp.setToolTip(tr("Copy"))
            cp.clicked.connect(lambda _=False, s=text: QGuiApplication.clipboard().setText(s))
            self.grid.addWidget(k, r, 0)
            self.grid.addWidget(val, r, 1)
            self.grid.addWidget(cp, r, 2)
        self.grid.setColumnStretch(1, 1)

    # ---- map -----------------------------------------------------------------
    def _to_canvas(self, lon, lat):
        canvas = self.iface.mapCanvas()
        t = QgsCoordinateTransform(coords.WGS84, canvas.mapSettings().destinationCrs(), QgsProject.instance())
        return t.transform(QgsPointXY(lon, lat))

    def go_to(self):
        pos = self.position or self.convert()
        if pos is None:
            return
        canvas = self.iface.mapCanvas()
        p = self._to_canvas(*pos)
        if self.marker is None:
            self.marker = QgsVertexMarker(canvas)
            self.marker.setIconType(QgsVertexMarker.IconType.ICON_CROSS)
            self.marker.setIconSize(22)
            self.marker.setPenWidth(3)
            self.marker.setColor(QColor("#D64545"))
        self.marker.setCenter(p)
        self.marker.show()
        if canvas.scale() > 20000 or not canvas.extent().contains(p):
            canvas.zoomScale(5000)
        canvas.setCenter(p)
        canvas.refresh()

    def add_point(self):
        pos = self.position or self.convert()
        if pos is None:
            return
        prj = QgsProject.instance()
        if self.layer is None or prj.mapLayer(self.layer.id()) is None:
            self.layer = coords.points_layer(tr("Coordinate points"))
            prj.addMapLayer(self.layer)
        coords.add_point(self.layer, self.input.text().strip(), *pos)
        self.go_to()

    def _toggle_pick(self, on):
        canvas = self.iface.mapCanvas()
        if on:
            self._prev_tool = canvas.mapTool()
            self.tool = QgsMapToolEmitPoint(canvas)
            self.tool.canvasClicked.connect(self._picked)
            canvas.setMapTool(self.tool)
        else:
            if self.tool is not None and canvas.mapTool() is self.tool:
                canvas.unsetMapTool(self.tool)
                if self._prev_tool is not None:
                    canvas.setMapTool(self._prev_tool)
            self.tool = None

    def _picked(self, point, _button):
        canvas = self.iface.mapCanvas()
        t = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(), coords.WGS84, QgsProject.instance())
        p = t.transform(point)
        self.input.setText(f"{p.y():.7f}, {p.x():.7f}")
        self.go_to()

    def hideEvent(self, e):  # noqa: N802
        if self.pick.isChecked():
            self.pick.setChecked(False)
        if self.marker is not None:
            self.marker.hide()
        super().hideEvent(e)

    def shutdown(self):
        if self.pick.isChecked():
            self.pick.setChecked(False)
        if self.marker is not None:
            self.iface.mapCanvas().scene().removeItem(self.marker)
            self.marker = None
