"""Parcels affected by a linear project (road, pipeline, power line): register and atlas.

The route is buffered into a corridor; every parcel it touches gets the area
taken, its share of the parcel, the length of route inside it and the owner.
The register goes to Excel and the atlas to a PDF - one page per parcel, or
strip sheets along the route - for land acquisition (France, Germany) and
expropriation files (نزع الملكية).
"""
import math
import os

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureRequest,
                       QgsFeatureSink, QgsField, QgsFields, QgsFillSymbol, QgsGeometry, QgsLayoutExporter,
                       QgsLayoutItemLabel, QgsLayoutItemMap, QgsLayoutItemPicture, QgsLayoutItemScaleBar,
                       QgsLayoutPoint, QgsLayoutSize, QgsLineSymbol, QgsPalLayerSettings, QgsPrintLayout,
                       QgsProcessingAlgorithm, QgsProcessingException,
                       QgsProcessingParameterBoolean, QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterNumber, QgsProject,
                       QgsRectangle, QgsRuleBasedRenderer, QgsTextFormat, QgsUnitTypes,
                       QgsVectorLayer, QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtCore import QMetaType
from qgis.PyQt.QtGui import QColor, QFont

from ..i18n import is_rtl, tr
from .base import BiruniAlgorithm, polygonal

MODES = ["One page per parcel", "Strip sheets along the route"]
PAPERS = {"A4": (297, 210), "A3": (420, 297)}


def _utm_for(extent_wgs84):
    c = extent_wgs84.center()
    zone = int(math.floor((c.x() + 180) / 6)) % 60 + 1
    return QgsCoordinateReferenceSystem(f"EPSG:{(32700 if c.y() < 0 else 32600) + zone}")


def _work_crs(crs, extent):
    if crs.isValid() and not crs.isGeographic() and crs.authid() not in ("EPSG:3857", "EPSG:900913"):
        return crs
    xf = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
    return _utm_for(xf.transformBoundingBox(extent))


class ParcelAtlas(BiruniAlgorithm):
    ID = "parcelatlas"
    NAME = "Parcels along a project (route) atlas"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("For a road, pipeline or power line: every parcel inside the corridor with the area taken, its share "
            "of the parcel, the length of route crossing it and the owner. Writes the parcel register to Excel and "
            "an atlas PDF - one page per parcel or strip sheets along the route - for land acquisition and "
            "expropriation files.")

    ROUTE, SELECTED, WIDTH, PARCELS, ID_FIELD, OWNER_FIELD = "ROUTE", "SELECTED", "WIDTH", "PARCELS", "ID_FIELD", \
        "OWNER_FIELD"
    MODE, SHEET_LENGTH, PAPER, OUTPUT, CORRIDOR, XLSX, PDF = "MODE", "SHEET_LENGTH", "PAPER", "OUTPUT", \
        "CORRIDOR", "XLSX", "PDF"

    def flags(self):
        return super().flags() | QgsProcessingAlgorithm.Flag.FlagNoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.ROUTE, tr("Route (road, pipeline, power line)"), [Qgis.ProcessingSourceType.VectorLine]))
        self.addParameter(QgsProcessingParameterNumber(
            self.WIDTH, tr("Corridor width (m, total)"), Qgis.ProcessingNumberParameterType.Double, 20.0,
            minValue=0.01))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.PARCELS, tr("Parcels"), [Qgis.ProcessingSourceType.VectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            self.ID_FIELD, tr("Parcel number field"), parentLayerParameterName=self.PARCELS, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.OWNER_FIELD, tr("Owner field"), parentLayerParameterName=self.PARCELS, optional=True))
        self.addParameter(QgsProcessingParameterEnum(self.MODE, tr("Atlas pages"), [tr(m) for m in MODES],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.SHEET_LENGTH, tr("Strip sheet length along the route (m)"), Qgis.ProcessingNumberParameterType.Double,
            500.0, minValue=10.0))
        self.addParameter(QgsProcessingParameterEnum(self.PAPER, tr("Paper"), list(PAPERS), defaultValue=0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Affected parcels"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.CORRIDOR, tr("Corridor"), Qgis.ProcessingSourceType.VectorPolygon, optional=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.XLSX, tr("Parcel register (Excel)"), "Excel (*.xlsx)", optional=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.PDF, tr("Atlas (PDF)"), "PDF (*.pdf)", optional=True))

    # ---- computation ---------------------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        route = self.parameterAsSource(parameters, self.ROUTE, context)
        parcels = self.parameterAsSource(parameters, self.PARCELS, context)
        if route is None or parcels is None:
            raise QgsProcessingException(tr("Choose the route and the parcels."))
        width = self.parameterAsDouble(parameters, self.WIDTH, context)
        id_field = (self.parameterAsStrings(parameters, self.ID_FIELD, context) or [None])[0]
        owner_field = (self.parameterAsStrings(parameters, self.OWNER_FIELD, context) or [None])[0]
        mode = self.parameterAsEnum(parameters, self.MODE, context)
        sheet_len = self.parameterAsDouble(parameters, self.SHEET_LENGTH, context)
        paper = list(PAPERS)[self.parameterAsEnum(parameters, self.PAPER, context)]

        work = _work_crs(parcels.sourceCrs(), parcels.sourceExtent())
        tc = context.transformContext()
        to_work_r = QgsCoordinateTransform(route.sourceCrs(), work, tc)
        to_work_p = QgsCoordinateTransform(parcels.sourceCrs(), work, tc)
        to_out = QgsCoordinateTransform(work, parcels.sourceCrs(), tc)

        lines = []
        for f in route.getFeatures():
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.isEmpty():
                continue
            g.transform(to_work_r)
            lines.append(g)
        if not lines:
            raise QgsProcessingException(tr("The route has no lines."))
        line = QgsGeometry.unaryUnion(lines)
        merged = line.mergeLines()
        if not merged.isEmpty():
            line = merged
        corridor = line.buffer(width / 2.0, 8, Qgis.EndCapStyle.Flat, Qgis.JoinStyle.Round, 2.0)
        if corridor.isEmpty():
            corridor = line.buffer(width / 2.0, 8)

        fields = QgsFields()
        for name, typ in (("parcel_id", QMetaType.Type.QString), ("owner", QMetaType.Type.QString),
                          ("parcel_m2", QMetaType.Type.Double), ("taken_m2", QMetaType.Type.Double),
                          ("taken_pct", QMetaType.Type.Double), ("crossed_m", QMetaType.Type.Double),
                          ("chainage_m", QMetaType.Type.Double), ("sheet", QMetaType.Type.Int)):
            fields.append(QgsField(name, typ))
        sink, sink_id = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.MultiPolygon,
                                             parcels.sourceCrs())

        engine = QgsGeometry.createGeometryEngine(corridor.constGet())
        engine.prepareGeometry()
        req = QgsFeatureRequest().setFilterRect(
            QgsCoordinateTransform(work, parcels.sourceCrs(), tc).transformBoundingBox(corridor.boundingBox()))
        rows = []
        all_parcels = []  # (geom in work crs, id) for the atlas background
        total = parcels.featureCount() or 1
        for k, f in enumerate(parcels.getFeatures(req)):
            if feedback.isCanceled():
                return {}
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.isEmpty():
                continue
            g.transform(to_work_p)
            if not g.isGeosValid():
                g = g.makeValid()
            pid = str(f[id_field]) if id_field and f[id_field] is not None else str(f.id())
            all_parcels.append((g, pid))
            if not engine.intersects(g.constGet()):
                continue
            taken = polygonal(g.intersection(corridor))
            if taken is None or taken.area() < 0.01:
                continue
            crossed = g.intersection(line)
            chain = line.lineLocatePoint(taken.pointOnSurface()) if line.type() == Qgis.GeometryType.Line else 0
            owner = str(f[owner_field]) if owner_field and f[owner_field] is not None else ""
            rows.append({"geom": g, "taken": taken, "pid": pid, "owner": owner, "parcel_m2": g.area(),
                         "taken_m2": taken.area(), "crossed_m": crossed.length() if not crossed.isEmpty() else 0.0,
                         "chainage_m": chain if chain >= 0 else 0.0})
            feedback.setProgress(60 * k / total)
        rows.sort(key=lambda r: r["chainage_m"])

        sheets = self._sheets(line, sheet_len, width) if mode == 1 else []
        for r in rows:
            # the sheet whose stretch of route the parcel's taken part sits on
            r["sheet"] = min(len(sheets), int(r["chainage_m"] // sheet_len) + 1) if sheets else 0
            out = QgsFeature(fields)
            og = QgsGeometry(r["geom"])
            og.transform(to_out)
            og.convertToMultiType()
            out.setGeometry(og)
            out.setAttributes([r["pid"], r["owner"], round(r["parcel_m2"], 2), round(r["taken_m2"], 2),
                               round(100.0 * r["taken_m2"] / r["parcel_m2"], 2) if r["parcel_m2"] else 0,
                               round(r["crossed_m"], 2), round(r["chainage_m"], 1), r["sheet"]])
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)

        results = {self.OUTPUT: sink_id, "PARCEL_COUNT": len(rows),
                   "TAKEN_M2": round(sum(r["taken_m2"] for r in rows), 2), "CORRIDOR_M2": round(corridor.area(), 2)}
        if parameters.get(self.CORRIDOR) is not None:
            cf = QgsFields()
            cf.append(QgsField("width_m", QMetaType.Type.Double))
            cf.append(QgsField("area_m2", QMetaType.Type.Double))
            csink, cid = self.parameterAsSink(parameters, self.CORRIDOR, context, cf, Qgis.WkbType.MultiPolygon,
                                              parcels.sourceCrs())
            if csink is not None:
                cg = QgsGeometry(corridor)
                cg.transform(to_out)
                cg.convertToMultiType()
                cfeat = QgsFeature(cf)
                cfeat.setGeometry(cg)
                cfeat.setAttributes([width, round(corridor.area(), 2)])
                csink.addFeature(cfeat)
                results[self.CORRIDOR] = cid

        xlsx = self.parameterAsFileOutput(parameters, self.XLSX, context)
        if xlsx:
            self._write_excel(xlsx, rows, width, corridor.area())
            results[self.XLSX] = xlsx
        feedback.setProgress(70)
        pdf = self.parameterAsFileOutput(parameters, self.PDF, context)
        if pdf and rows:
            pages = self._write_atlas(pdf, work, rows, all_parcels, line, corridor, sheets, mode, paper, feedback)
            results[self.PDF] = pdf
            results["PAGES"] = pages
        feedback.pushInfo(tr("{n} parcels affected, {a} m² taken.").format(n=len(rows),
                                                                             a=f"{results['TAKEN_M2']:,.2f}"))
        return results

    @staticmethod
    def _sheets(line, length, width):
        """Strip sheets along the route: (polygon covering the stretch, direction angle in degrees)."""
        total = line.length()
        out = []
        start = 0.0
        while start < total - 1e-6:
            end = min(total, start + length)
            a = line.interpolate(start).asPoint()
            b = line.interpolate(end).asPoint()
            part = QgsGeometry.fromPolylineXY([line.interpolate(start + (end - start) * t / 20).asPoint()
                                               for t in range(21)])
            angle = math.degrees(math.atan2(b.y() - a.y(), b.x() - a.x()))
            out.append((part.buffer(max(width * 1.5, length * 0.18), 4), angle))
            start = end
        return out

    # ---- outputs -------------------------------------------------------------------------------
    @staticmethod
    def _write_excel(path, rows, width, corridor_area):
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill

        wb = Workbook()
        ws = wb.active
        ws.title = tr("Parcel register")[:31]
        rtl = is_rtl()
        ws.sheet_view.rightToLeft = rtl
        heads = [tr("No."), tr("Parcel"), tr("Owner"), tr("Parcel area (m²)"), tr("Area taken (m²)"),
                 tr("Share taken (%)"), tr("Route inside the parcel (m)"), tr("Chainage (m)"), tr("Sheet")]
        ws.append(heads)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="2F6B5E")
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for n, r in enumerate(rows, 1):
            ws.append([n, r["pid"], r["owner"], round(r["parcel_m2"], 2), round(r["taken_m2"], 2),
                       round(100 * r["taken_m2"] / r["parcel_m2"], 2) if r["parcel_m2"] else 0,
                       round(r["crossed_m"], 2), round(r["chainage_m"], 1), r.get("sheet") or ""])
        last = len(rows) + 1
        ws.append([])
        ws.append(["", tr("Total"), "", f"=SUM(D2:D{last})", f"=SUM(E2:E{last})", "", f"=SUM(G2:G{last})"])
        ws.append(["", tr("Corridor width (m)"), width])
        ws.append(["", tr("Corridor area (m²)"), round(corridor_area, 2)])
        for row in ws.iter_rows(min_row=last + 2, max_row=last + 2):
            for c in row:
                c.font = Font(bold=True)
        for col, wdt in zip("ABCDEFGHI", (6, 14, 28, 16, 16, 14, 18, 13, 8)):
            ws.column_dimensions[col].width = wdt
        for row in ws.iter_rows(min_row=2, max_row=last):
            for c in row[3:8]:
                c.number_format = "#,##0.00"
        ws.freeze_panes = "A2"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        wb.save(path)

    def _write_atlas(self, path, crs, rows, all_parcels, line, corridor, sheets, mode, paper, feedback):
        project = QgsProject()
        project.setCrs(crs)
        auth = crs.authid() or crs.toWkt()

        def mem(kind, name, flds):
            spec = f"{kind}?crs={auth}" + "".join(f"&field={n}:{t}" for n, t in flds)
            return QgsVectorLayer(spec, name, "memory")

        bg = mem("MultiPolygon", tr("Parcels"), [("pid", "string")])
        feats = []
        for g, pid in all_parcels:
            f = QgsFeature(bg.fields())
            gg = QgsGeometry(g)
            gg.convertToMultiType()
            f.setGeometry(gg)
            f.setAttributes([pid])
            feats.append(f)
        bg.dataProvider().addFeatures(feats)
        bg.renderer().setSymbol(QgsFillSymbol.createSimple({"color": "255,255,255,0", "outline_color": "#5B6670",
                                                            "outline_width": "0.2"}))
        pal = QgsPalLayerSettings()
        pal.fieldName = "pid"
        fmt = QgsTextFormat()
        fmt.setFont(QFont("Segoe UI"))
        fmt.setSize(7)
        fmt.setColor(QColor("#3A444D"))
        pal.setFormat(fmt)
        bg.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        bg.setLabelsEnabled(True)

        cor = mem("MultiPolygon", tr("Corridor"), [])
        cf = QgsFeature(cor.fields())
        cg = QgsGeometry(corridor)
        cg.convertToMultiType()
        cf.setGeometry(cg)
        cor.dataProvider().addFeatures([cf])
        cor.renderer().setSymbol(QgsFillSymbol.createSimple({"color": "214,69,69,70", "outline_color": "#D64545",
                                                             "outline_width": "0.3"}))
        rl = mem("MultiLineString", tr("Route"), [])
        rf = QgsFeature(rl.fields())
        lg = QgsGeometry(line)
        lg.convertToMultiType()
        rf.setGeometry(lg)
        rl.dataProvider().addFeatures([rf])
        rl.renderer().setSymbol(QgsLineSymbol.createSimple({"color": "#B3261E", "width": "0.6", "line_style": "dash"}))

        if mode == 0:
            cov = mem("MultiPolygon", tr("Affected parcels"),
                      [("pid", "string"), ("owner", "string"), ("parcel_m2", "double"), ("taken_m2", "double"),
                       ("taken_pct", "double"), ("crossed_m", "double")])
            cfeats = []
            for r in rows:
                f = QgsFeature(cov.fields())
                gg = QgsGeometry(r["geom"])
                gg.convertToMultiType()
                f.setGeometry(gg)
                f.setAttributes([r["pid"], r["owner"], round(r["parcel_m2"], 2), round(r["taken_m2"], 2),
                                 round(100 * r["taken_m2"] / r["parcel_m2"], 2) if r["parcel_m2"] else 0,
                                 round(r["crossed_m"], 2)])
                cfeats.append(f)
            cov.dataProvider().addFeatures(cfeats)
            root = QgsRuleBasedRenderer.Rule(None)
            cur = QgsRuleBasedRenderer.Rule(QgsFillSymbol.createSimple({"color": "255,214,102,120",
                                                                        "outline_color": "#1E252B",
                                                                        "outline_width": "0.6"}),
                                            filterExp="$id = @atlas_featureid")
            root.appendChild(cur)
            cov.setRenderer(QgsRuleBasedRenderer(root))
        else:
            cov = mem("Polygon", tr("Sheets"), [("sheet", "integer"), ("angle", "double")])
            cfeats = []
            for n, (sg, ang) in enumerate(sheets, 1):
                f = QgsFeature(cov.fields())
                f.setGeometry(sg)
                f.setAttributes([n, ang])
                cfeats.append(f)
            cov.dataProvider().addFeatures(cfeats)
            cov.renderer().setSymbol(QgsFillSymbol.createSimple({"color": "0,0,0,0", "outline_style": "no"}))
        layers = [cov, rl, cor, bg]
        project.addMapLayers(layers)

        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        pw, ph = PAPERS[paper]
        layout.pageCollection().page(0).setPageSize(QgsLayoutSize(pw, ph))
        mp = QgsLayoutItemMap(layout)
        mp.setFrameEnabled(True)
        layout.addLayoutItem(mp)
        mp.attemptMove(QgsLayoutPoint(10, 22))
        mp.attemptResize(QgsLayoutSize(pw - 20, ph - 40))
        mp.setCrs(crs)
        mp.setLayers(layers)
        mp.setKeepLayerSet(True)
        mp.zoomToExtent(QgsRectangle(corridor.boundingBox()))
        mp.setAtlasDriven(True)
        mp.setAtlasScalingMode(QgsLayoutItemMap.AtlasScalingMode.Auto)
        mp.setAtlasMargin(0.25)

        title = QgsLayoutItemLabel(layout)
        if mode == 0:
            title.setText(tr("Parcel") + " [% \"pid\" %]  ·  " + tr("Owner") + ": [% \"owner\" %]\n"
                          + tr("Area taken") + ": [% format_number(\"taken_m2\", 2) %] m²  ("
                          "[% format_number(\"taken_pct\", 2) %] %)  ·  " + tr("Route inside the parcel")
                          + ": [% format_number(\"crossed_m\", 2) %] m")
        else:
            title.setText(tr("Sheet") + " [% \"sheet\" %] / [% @atlas_totalfeatures %]")
        tf = QgsTextFormat()
        tf.setFont(QFont("Segoe UI"))
        tf.setSize(10)
        tf.setColor(QColor("#1E252B"))
        title.setTextFormat(tf)
        layout.addLayoutItem(title)
        title.attemptMove(QgsLayoutPoint(10, 6))
        title.attemptResize(QgsLayoutSize(pw - 20, 14))

        sb = QgsLayoutItemScaleBar(layout)
        sb.setStyle("Single Box")
        sb.setLinkedMap(mp)
        sb.setUnits(QgsUnitTypes.DistanceUnit.DistanceMeters)
        sb.setUnitLabel("m")
        sb.setNumberOfSegments(2)
        sb.setNumberOfSegmentsLeft(0)
        sb.applyDefaultSize(QgsUnitTypes.DistanceUnit.DistanceMeters)
        layout.addLayoutItem(sb)
        sb.attemptMove(QgsLayoutPoint(10, ph - 15))
        page = QgsLayoutItemLabel(layout)
        page.setText(tr("Page") + " [% @atlas_featurenumber %] / [% @atlas_totalfeatures %]")
        page.setTextFormat(tf)
        layout.addLayoutItem(page)
        page.attemptMove(QgsLayoutPoint(pw - 60, ph - 13))
        page.attemptResize(QgsLayoutSize(50, 6))
        arrow = QgsLayoutItemPicture(layout)
        from ..mapsheet import _north_arrow_svg

        svg = _north_arrow_svg()
        if svg:
            arrow.setPicturePath(svg)
        arrow.setLinkedMap(mp)
        layout.addLayoutItem(arrow)
        arrow.attemptMove(QgsLayoutPoint(pw - 22, 24))
        arrow.attemptResize(QgsLayoutSize(10, 13))

        atlas = layout.atlas()
        atlas.setCoverageLayer(cov)
        atlas.setEnabled(True)
        atlas.setHideCoverage(mode == 1)
        settings = QgsLayoutExporter.PdfExportSettings()
        settings.rasterizeWholeImage = False
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        res, err = QgsLayoutExporter.exportToPdf(atlas, path, settings)
        if res != QgsLayoutExporter.ExportResult.Success:
            raise QgsProcessingException(tr("The atlas could not be written: {e}").format(e=err))
        return cov.featureCount()
