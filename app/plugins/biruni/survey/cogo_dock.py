"""COGO panel: type bearings and distances (from a deed or a field book), see
the figure live on the map with its misclosure, and create the parcel."""
import datetime as dt

from qgis.core import (Qgis, QgsAbstractGeometry, QgsCoordinateTransform, QgsFeature, QgsField, QgsFields,
                       QgsGeometry, QgsPointXY, QgsProject, QgsVectorFileWriter, QgsVectorLayer,
                       QgsVectorLayerUtils, QgsWkbTypes)
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand
from qgis.PyQt.QtCore import QMetaType, Qt
from qgis.PyQt.QtGui import QColor, QKeySequence
from qgis.PyQt.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDockWidget, QFrame, QGridLayout,
                                 QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea,
                                 QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import icons
from ..i18n import tr
from .cogo import Traverse, course_from_text, normalize_digits
from .cogo_geom import traverse_geometry

TABLES = {"polygon": ("cogo_parcels", "COGO parcels"), "line": ("cogo_lines", "COGO lines")}


def panel_qss(t):
    return f"""
#BiruniCogo QLabel#CogoHint {{ color: {t['text_dim']}; }}
#BiruniCogo QLabel#CogoResult {{ font-weight: 600; color: {t['text']}; }}
#BiruniCogo QLabel#CogoBad {{ font-weight: 600; color: #D64545; }}
#BiruniCogo QFrame#CogoCard {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 8px; }}
#BiruniCogo QPushButton#CogoPrimary {{ background: {t['accent']}; color: {t['accent_text']}; border: none;
    border-radius: 6px; padding: 7px 14px; font-weight: 600; }}
#BiruniCogo QPushButton#CogoPrimary:hover {{ background: {t['accent_hover']}; }}
#BiruniCogo QPushButton#CogoPrimary:disabled {{ background: {t['mid']}; }}
"""


class CourseTable(QTableWidget):
    """Bearing / distance / radius grid that accepts rows pasted from Excel."""

    def __init__(self, parent=None):
        super().__init__(0, 3, parent)
        self.setHorizontalHeaderLabels([tr("Bearing"), tr("Distance"), tr("Radius")])
        h = self.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.verticalHeader().setDefaultSectionSize(24)

    def keyPressEvent(self, e):  # noqa: N802
        if e.matches(QKeySequence.StandardKey.Paste):
            self.paste(QApplication.clipboard().text())
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentRow() == self.rowCount() - 1 \
                and self.currentColumn() == 1:
            self.add_row()
            self.setCurrentCell(self.rowCount() - 1, 0)
            return
        super().keyPressEvent(e)

    def add_row(self, values=("", "", "")):
        r = self.rowCount()
        self.insertRow(r)
        for c, v in enumerate(values):
            self.setItem(r, c, QTableWidgetItem(str(v)))
        return r

    def paste(self, text):
        rows = [ln for ln in (text or "").splitlines() if ln.strip()]

        def empty(r):
            return not any(self.item(r, c) and self.item(r, c).text().strip() for c in range(3))

        # Fill from the current row when it is empty, else after the last used row.
        start = self.currentRow()
        if start < 0 or not empty(start):
            start = self.rowCount()
            while start > 0 and empty(start - 1):
                start -= 1
        for k, ln in enumerate(rows):
            cells = ln.split("\t") if "\t" in ln else [p for p in ln.replace(";", ",").split(",")]
            cells = [c.strip() for c in cells][:3] + [""] * max(0, 3 - len(cells))
            r = start + k
            if r >= self.rowCount():
                self.insertRow(r)
            for c in range(3):
                self.setItem(r, c, QTableWidgetItem(cells[c]))

    def courses(self):
        """(courses, errors) - errors are row numbers that cannot be read."""
        out, bad = [], []
        for r in range(self.rowCount()):
            vals = [(self.item(r, c).text().strip() if self.item(r, c) else "") for c in range(3)]
            if not vals[0] and not vals[1]:
                continue
            try:
                out.append(course_from_text(vals[0], vals[1], vals[2] or None))
            except ValueError:
                bad.append(r + 1)
        return out, bad


class CogoDock(QDockWidget):
    def __init__(self, plugin):
        super().__init__(tr("COGO - Bearings & Distances"), plugin.iface.mainWindow())
        self.plugin = plugin
        self.iface = plugin.iface
        self.setObjectName("BiruniCogo")
        self.band = None
        self.start_marker = None
        self.pick_tool = None
        self._prev_tool = None
        self.traverse = None
        self._build()

    # ---- UI --------------------------------------------------------------
    def _build(self):
        body = QWidget()
        body.setObjectName("BiruniCogo")
        v = QVBoxLayout(body)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(8)
        t = self.plugin.theme.tokens()

        start = QFrame()
        start.setObjectName("CogoCard")
        g = QGridLayout(start)
        g.addWidget(QLabel(tr("Start point")), 0, 0, 1, 3)
        self.x = QLineEdit()
        self.x.setPlaceholderText(tr("X / Easting"))
        self.y = QLineEdit()
        self.y.setPlaceholderText(tr("Y / Northing"))
        pick = QPushButton(icons.icon("target", t["icon"]), "")
        pick.setToolTip(tr("Pick the start point on the map"))
        pick.clicked.connect(self.pick_start)
        g.addWidget(self.x, 1, 0)
        g.addWidget(self.y, 1, 1)
        g.addWidget(pick, 1, 2)
        self.crs_label = QLabel()
        self.crs_label.setObjectName("CogoHint")
        self.crs_label.setWordWrap(True)
        g.addWidget(self.crs_label, 2, 0, 1, 3)
        v.addWidget(start)

        self.table = CourseTable()
        for _ in range(4):
            self.table.add_row()
        v.addWidget(self.table, 1)
        hint = QLabel(tr("Bearings: 123.5, 123°30'15\", 123 30 15 or N45°30'E. Radius (optional) makes the course "
                         "an arc: negative or L = curving left. You can paste rows from Excel (Ctrl+V)."))
        hint.setObjectName("CogoHint")
        hint.setWordWrap(True)
        v.addWidget(hint)
        rows = QHBoxLayout()
        add = QPushButton(icons.icon("square-rounded-plus", t["icon"]), tr("Add course"))
        add.clicked.connect(lambda: self.table.add_row())
        rem = QPushButton(icons.icon("trash", t["icon"]), "")
        rem.setToolTip(tr("Remove the selected course"))
        rem.clicked.connect(self._remove_row)
        clear = QPushButton(icons.icon("refresh", t["icon"]), "")
        clear.setToolTip(tr("Clear all courses"))
        clear.clicked.connect(self._clear)
        rows.addWidget(add, 1)
        rows.addWidget(rem)
        rows.addWidget(clear)
        v.addLayout(rows)

        opts = QHBoxLayout()
        self.closed = QCheckBox(tr("Closed (returns to the start)"))
        self.closed.setChecked(True)
        self.adjust = QCheckBox(tr("Distribute the misclosure"))
        self.adjust.setChecked(True)
        opts.addWidget(self.closed)
        opts.addWidget(self.adjust)
        v.addLayout(opts)

        res = QFrame()
        res.setObjectName("CogoCard")
        rv = QVBoxLayout(res)
        self.result = QLabel(tr("Enter the start point and the courses."))
        self.result.setObjectName("CogoHint")
        self.result.setWordWrap(True)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        rv.addWidget(self.result)
        v.addWidget(res)

        make = QHBoxLayout()
        self.make_poly = QPushButton(icons.icon("vector-triangle", t["icon"]), tr("Create polygon"))
        self.make_poly.setObjectName("CogoPrimary")
        self.make_poly.clicked.connect(lambda: self.create("polygon"))
        self.make_line = QPushButton(icons.icon("route", t["icon"]), tr("Create line"))
        self.make_line.clicked.connect(lambda: self.create("line"))
        make.addWidget(self.make_poly, 1)
        make.addWidget(self.make_line, 1)
        v.addLayout(make)

        # A short dock (stacked with other panels) scrolls instead of squashing the fields
        self.table.setMinimumHeight(150)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        self.setWidget(scroll)
        self.setMinimumWidth(300)
        self.table.itemChanged.connect(self.update_preview)
        for w in (self.x, self.y):
            w.textChanged.connect(self.update_preview)
        self.closed.toggled.connect(self.update_preview)
        self.adjust.toggled.connect(self.update_preview)
        self.visibilityChanged.connect(self._on_visibility)
        self._update_crs()

    def refresh_theme(self):
        self.widget().setStyleSheet(panel_qss(self.plugin.theme.tokens()))
        if self.band is not None:
            self._style_band()

    def _crs(self):
        return self.iface.mapCanvas().mapSettings().destinationCrs()

    def _update_crs(self):
        crs = self._crs()
        if crs.isGeographic():
            self.crs_label.setText(tr("The map is in degrees ({c}). Set the project to a projected coordinate "
                                      "system in metres (e.g. UTM) to lay out distances.").format(c=crs.authid()))
        else:
            self.crs_label.setText(tr("Coordinates in {c}").format(c=f"{crs.authid()} - {crs.description()}"))

    def _on_visibility(self, visible):
        if visible:
            self._update_crs()
            self.update_preview()
        else:
            self._clear_band()

    def _remove_row(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)
            self.update_preview()

    def _clear(self):
        self.table.setRowCount(0)
        for _ in range(4):
            self.table.add_row()
        self.update_preview()

    # ---- start point ---------------------------------------------------------
    def pick_start(self):
        canvas = self.iface.mapCanvas()
        self._prev_tool = canvas.mapTool()
        self.pick_tool = QgsMapToolEmitPoint(canvas)
        self.pick_tool.canvasClicked.connect(self._picked)
        canvas.setMapTool(self.pick_tool)

    def _picked(self, point, _button):
        self.x.setText(f"{point.x():.4f}")
        self.y.setText(f"{point.y():.4f}")
        canvas = self.iface.mapCanvas()
        if self._prev_tool is not None:
            canvas.setMapTool(self._prev_tool)
        else:
            canvas.unsetMapTool(self.pick_tool)

    def _start(self):
        try:
            return float(normalize_digits(self.x.text()).replace(",", ".")), \
                float(normalize_digits(self.y.text()).replace(",", "."))
        except ValueError:
            return None

    # ---- computation & preview ------------------------------------------------
    def compute(self):
        start = self._start()
        courses, bad = self.table.courses()
        if start is None or not courses:
            return None, bad
        trav = Traverse(start, courses)
        if self.closed.isChecked() and self.adjust.isChecked() and len(courses) >= 2:
            trav.adjust()
        return trav, bad

    def update_preview(self, *_):
        self._update_crs()
        trav, bad = self.compute()
        self.traverse = trav
        if bad:
            self.result.setObjectName("CogoBad")
            self.result.setText(tr("Cannot read row(s): {rows}").format(rows=", ".join(map(str, bad))))
        elif trav is None:
            self.result.setObjectName("CogoHint")
            self.result.setText(tr("Enter the start point and the courses."))
        else:
            self.result.setObjectName("CogoResult")
            lines = [tr("{n} courses, length {L} m").format(n=len(trav.courses), L=f"{trav.total_length:.3f}")]
            if self.closed.isChecked() and len(trav.courses) >= 2:
                dx, dy, lin = trav.misclosure()
                prec = trav.precision()
                lines.append(tr("Misclosure: dX {dx}, dY {dy}, linear {e} m").format(
                    dx=f"{dx:+.4f}", dy=f"{dy:+.4f}", e=f"{lin:.4f}"))
                lines.append(tr("Precision 1:{p}").format(p="∞" if prec == float("inf") else f"{prec:,.0f}"))
                if len(trav.courses) >= 3:
                    lines.append(tr("Area {a} m² · perimeter {p} m").format(a=f"{trav.area():.4f}",
                                                                             p=f"{trav.perimeter():.3f}"))
            self.result.setText("\n".join(lines))
        self.result.style().unpolish(self.result)
        self.result.style().polish(self.result)
        can = trav is not None and not bad and not self._crs().isGeographic()
        self.make_poly.setEnabled(can and len(trav.courses) >= 2)
        self.make_line.setEnabled(can)
        self._draw(trav if not bad else None)

    def _style_band(self):
        accent = QColor(self.plugin.theme.tokens()["accent"])
        fill = QColor(accent)
        fill.setAlpha(45)
        self.band.setStrokeColor(accent)
        self.band.setFillColor(fill)
        self.band.setWidth(2)

    def _draw(self, trav):
        if trav is None or not self.isVisible():
            self._clear_band()
            return
        canvas = self.iface.mapCanvas()
        polygon = self.closed.isChecked() and len(trav.courses) >= 2
        geom = traverse_geometry(trav, polygon)
        if self.band is None:
            self.band = QgsRubberBand(canvas, Qgis.GeometryType.Polygon)
            self._style_band()
        self.band.reset(Qgis.GeometryType.Polygon if polygon else Qgis.GeometryType.Line)
        self._style_band()
        lin = QgsGeometry(geom.constGet().segmentize(0.01, QgsAbstractGeometry.SegmentationToleranceType.MaximumDifference))
        self.band.setToGeometry(lin, None)

    def _clear_band(self):
        if self.band is not None:
            self.iface.mapCanvas().scene().removeItem(self.band)
            self.band = None

    # ---- create ----------------------------------------------------------------
    def _target_layer(self, kind):
        """The active editable layer of the right geometry, else the project COGO layer."""
        want = Qgis.GeometryType.Polygon if kind == "polygon" else Qgis.GeometryType.Line
        active = self.iface.activeLayer()
        if isinstance(active, QgsVectorLayer) and active.isEditable() and active.geometryType() == want:
            return active
        table, title = TABLES[kind]
        prj = QgsProject.instance()
        for lyr in prj.mapLayers().values():
            if isinstance(lyr, QgsVectorLayer) and lyr.customProperty("biruni/cogo") == kind and lyr.isValid():
                return lyr
        fields = QgsFields()
        for n, t in (("area", QMetaType.Type.Double), ("length", QMetaType.Type.Double),
                     ("courses", QMetaType.Type.Int), ("misclosure", QMetaType.Type.Double),
                     ("precision", QMetaType.Type.QString), ("created", QMetaType.Type.QString)):
            fields.append(QgsField(n, t))
        wkb = Qgis.WkbType.CurvePolygon if kind == "polygon" else Qgis.WkbType.CompoundCurve
        layer = None
        try:
            from ..project import project_gpkg

            gpkg = project_gpkg()
        except Exception:
            gpkg = None
        if gpkg:
            uri = f"{gpkg}|layername={table}"
            layer = QgsVectorLayer(uri, tr(title), "ogr")
            if not layer.isValid():
                opts = QgsVectorFileWriter.SaveVectorOptions()
                opts.driverName, opts.layerName = "GPKG", table
                opts.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                w = QgsVectorFileWriter.create(gpkg, fields, wkb, self._crs(), prj.transformContext(), opts)
                del w
                layer = QgsVectorLayer(uri, tr(title), "ogr")
        if layer is None or not layer.isValid():
            layer = QgsVectorLayer(f"{QgsWkbTypes.displayString(wkb)}?crs={self._crs().authid()}", tr(title),
                                   "memory")
            layer.dataProvider().addAttributes(fields.toList())
            layer.updateFields()
        layer.setCustomProperty("biruni/cogo", kind)
        prj.addMapLayer(layer)
        return layer

    def create(self, kind):
        trav, bad = self.compute()
        if trav is None or bad:
            return None
        polygon = kind == "polygon"
        geom = traverse_geometry(trav, polygon)
        layer = self._target_layer(kind)
        if layer.crs() != self._crs():
            geom.transform(QgsCoordinateTransform(self._crs(), layer.crs(), QgsProject.instance()))
        if not QgsWkbTypes.isCurvedType(layer.wkbType()) and geom.constGet().hasCurvedSegments():
            geom = QgsGeometry(geom.constGet().segmentize(
                0.0001, QgsAbstractGeometry.SegmentationToleranceType.MaximumDifference))
        if QgsWkbTypes.isMultiType(layer.wkbType()):
            geom.convertToMultiType()
        closed = self.closed.isChecked() and len(trav.courses) >= 2
        prec = trav.precision() if closed else None
        values = {"area": trav.area() if polygon else None, "length": trav.perimeter(), "courses": len(trav.courses),
                  "misclosure": trav.misclosure()[2] if closed else None,
                  "precision": None if prec is None else ("1:∞" if prec == float("inf") else f"1:{prec:.0f}"),
                  "created": dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")}
        attrs = {layer.fields().indexOf(k): v for k, v in values.items() if layer.fields().indexOf(k) >= 0}
        f = QgsVectorLayerUtils.createFeature(layer, geom, attrs)
        if layer.isEditable():
            ok = layer.addFeature(f)
        else:
            ok, _ = layer.dataProvider().addFeatures([f])
        layer.triggerRepaint()
        if ok:
            QgsProject.instance().setDirty(True)
            self.iface.messageBar().pushSuccess(tr("COGO"), tr("Added to {layer}.").format(layer=layer.name()))
            act = getattr(self.plugin, "activity", None)
            if act is not None:
                try:
                    act.log("cogo", "created", layer.name(), kind, 1)
                except Exception:
                    pass
        return f if ok else None

    def shutdown(self):
        self._clear_band()
        canvas = self.iface.mapCanvas()
        if self.pick_tool is not None and canvas.mapTool() is self.pick_tool:
            canvas.unsetMapTool(self.pick_tool)
