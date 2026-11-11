"""EU Deforestation Regulation (EUDR, Regulation (EU) 2023/1115) plot check and GeoJSON export.

Operators must give the geolocation of every plot that produced cattle, cocoa,
coffee, oil palm, rubber, soya or wood: WGS 84 latitude/longitude with at
least six decimal digits, a polygon for plots of more than four hectares
(a point is enough below that, except for cattle). Due-diligence systems
reject files with unclosed or self-intersecting polygons, overlapping plots,
swapped coordinates or missing properties. This tool finds those problems,
repairs the safe ones and writes a GeoJSON ready to upload.
"""
import json
import os
import re

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea, QgsFeature,
                       QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessingException,
                       QgsProcessingParameterBoolean, QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterString, QgsSpatialIndex,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

COMMODITIES = ["cattle", "cocoa", "coffee", "oil palm", "rubber", "soya", "wood"]
POLYGON_HA = 4.0          # above this a plot must be a polygon
DECIMALS = 6              # coordinate precision required
MAX_FILE_MB = 25          # upload limit of the EU information system (warning only)
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
RULES = ["No geometry", "Coordinates out of range", "Latitude and longitude swapped", "Polygon not closed",
         "Self-intersecting or invalid polygon", "Too few corners", "Plot over 4 ha given as a point",
         "Cattle plot given as a point", "Overlaps another plot of the same producer", "Plot area is zero",
         "Missing producer", "Country is not an ISO code", "Country does not match the plot location",
         "Unknown commodity"]


def _ring_closed(ring):
    return len(ring) >= 2 and ring[0].x() == ring[-1].x() and ring[0].y() == ring[-1].y()


def _round_point(p, d=DECIMALS):
    return [round(p.x(), d), round(p.y(), d)]


def _signed_area(ring):
    return sum(ring[k][0] * ring[k + 1][1] - ring[k + 1][0] * ring[k][1] for k in range(len(ring) - 1)) / 2


def ring_coords(ring, exterior):
    """Rounded GeoJSON ring: closed, exterior counter-clockwise and holes clockwise (RFC 7946)."""
    pts = [_round_point(p) for p in ring]
    dedup = [pts[0]]
    for p in pts[1:]:
        if p != dedup[-1]:
            dedup.append(p)
    if dedup[0] != dedup[-1]:
        dedup.append(dedup[0])
    ccw = _signed_area(dedup) > 0
    if ccw != exterior:
        dedup.reverse()
    return dedup


def geojson_geometry(geom):
    """GeoJSON geometry dict of a WGS 84 QgsGeometry with 6-decimal coordinates."""
    if geom.type() == Qgis.GeometryType.Point:
        if geom.isMultipart():
            return {"type": "MultiPoint", "coordinates": [_round_point(p) for p in geom.asMultiPoint()]}
        return {"type": "Point", "coordinates": _round_point(geom.asPoint())}
    polys = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    out = [[ring_coords(r, k == 0) for k, r in enumerate(poly)] for poly in polys if poly]
    if len(out) == 1:
        return {"type": "Polygon", "coordinates": out[0]}
    return {"type": "MultiPolygon", "coordinates": out}


def _countries():
    """ISO alpha-2 -> country polygon (WGS 84) from the bundled world data, if present."""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "data", "world.gpkg")
    lyr = QgsVectorLayer(f"{path}|layername=countries", "countries", "ogr")
    out = {}
    if lyr.isValid():
        for f in lyr.getFeatures():
            code = str(f["iso_a2"] or "").upper()
            if len(code) == 2 and code.isalpha():
                out.setdefault(code, []).append(QgsGeometry(f.geometry()))
    return {k: QgsGeometry.unaryUnion(v) for k, v in out.items()}


class EudrCheck(BiruniAlgorithm):
    ID = "eudrcheck"
    NAME = "EUDR plot check and GeoJSON"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Checks production plots for the EU Deforestation Regulation (EUDR) and writes the GeoJSON to upload: "
            "WGS 84 with six decimals, polygons for plots over 4 ha, closed rings without self-intersections, no "
            "overlaps between plots of the same producer, the producer, the country (ISO code, checked against the "
            "plot location) and the commodity. Safe problems are repaired; the others are listed.")

    INPUT, PLOT_ID, PRODUCER, COUNTRY_FIELD, COUNTRY, COMMODITY_FIELD, COMMODITY, AREA_FIELD, FIX = \
        "INPUT", "PLOT_ID", "PRODUCER", "COUNTRY_FIELD", "COUNTRY", "COMMODITY_FIELD", "COMMODITY", "AREA_FIELD", "FIX"
    GEOJSON, OUTPUT, PROBLEMS = "GEOJSON", "OUTPUT", "PROBLEMS"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, tr("Plots (polygons or points)"),
            [Qgis.ProcessingSourceType.VectorPolygon, Qgis.ProcessingSourceType.VectorPoint]))
        for name, label in ((self.PLOT_ID, tr("Plot ID field")), (self.PRODUCER, tr("Producer field")),
                            (self.COUNTRY_FIELD, tr("Country field (ISO code)")),
                            (self.COMMODITY_FIELD, tr("Commodity field")),
                            (self.AREA_FIELD, tr("Declared area field (ha, for points)"))):
            self.addParameter(QgsProcessingParameterField(name, label, parentLayerParameterName=self.INPUT,
                                                          optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.COUNTRY, tr("Country for all plots (ISO code, when there is no field)"), optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.COMMODITY, tr("Commodity for all plots (when there is no field)"), [tr(c) for c in COMMODITIES],
            defaultValue=2))
        self.addParameter(QgsProcessingParameterBoolean(
            self.FIX, tr("Repair what can be repaired safely (invalid polygons, swapped coordinates)"), True))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.GEOJSON, tr("EUDR GeoJSON"), "GeoJSON (*.geojson *.json)", optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Checked plots"), Qgis.ProcessingSourceType.VectorAnyGeometry, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.PROBLEMS, tr("Problems"), Qgis.ProcessingSourceType.VectorPoint))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        src = self.parameterAsSource(parameters, self.INPUT, context)
        if src is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))

        def field(name):
            f = self.parameterAsStrings(parameters, name, context)
            return f[0] if f else None

        f_id, f_prod, f_cc, f_com, f_area = (field(n) for n in (self.PLOT_ID, self.PRODUCER, self.COUNTRY_FIELD,
                                                                 self.COMMODITY_FIELD, self.AREA_FIELD))
        cc_all = (self.parameterAsString(parameters, self.COUNTRY, context) or "").strip().upper()
        com_all = COMMODITIES[self.parameterAsEnum(parameters, self.COMMODITY, context)]
        fix = self.parameterAsBool(parameters, self.FIX, context)
        xf = QgsCoordinateTransform(src.sourceCrs(), WGS84, context.transformContext())
        da = QgsDistanceArea()
        da.setSourceCrs(WGS84, context.transformContext())
        da.setEllipsoid("EPSG:7030")
        countries = _countries()

        pf = QgsFields()
        for n, t in (("plot", QMetaType.Type.QString), ("rule", QMetaType.Type.QString),
                     ("severity", QMetaType.Type.QString), ("detail", QMetaType.Type.QString),
                     ("fixed", QMetaType.Type.Bool)):
            pf.append(QgsField(n, t))
        psink, pid = self.parameterAsSink(parameters, self.PROBLEMS, context, pf, Qgis.WkbType.Point, WGS84)
        of = QgsFields()
        for n, t in (("plot_id", QMetaType.Type.QString), ("producer", QMetaType.Type.QString),
                     ("country", QMetaType.Type.QString), ("commodity", QMetaType.Type.QString),
                     ("area_ha", QMetaType.Type.Double), ("ok", QMetaType.Type.Bool),
                     ("problems", QMetaType.Type.QString)):
            of.append(QgsField(n, t))
        from qgis.core import QgsWkbTypes

        out_type = QgsWkbTypes.multiType(QgsWkbTypes.flatType(src.wkbType()))
        osink, oid = (self.parameterAsSink(parameters, self.OUTPUT, context, of, out_type, WGS84)
                      if parameters.get(self.OUTPUT) is not None else (None, None))

        problems = []

        def problem(plot, rule, detail, where=None, fixed=False, severity="error"):
            problems.append((plot, rule, severity, detail, fixed, where))

        plots = []
        total = src.featureCount() or 1
        for k, f in enumerate(src.getFeatures()):
            if feedback.isCanceled():
                return {}
            plot = str(f[f_id]) if f_id and f[f_id] is not None else str(f.id())
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.isEmpty():
                problem(plot, "No geometry", tr("The plot has no location."))
                continue
            # rings that are not closed (only possible in some source formats)
            if g.type() == Qgis.GeometryType.Polygon:
                parts = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
                if any(r and not _ring_closed(r) for poly in parts for r in poly):
                    problem(plot, "Polygon not closed", tr("A ring does not end where it starts."),
                            fixed=fix, severity="error" if not fix else "fixed")
            try:
                g.transform(xf)
            except Exception as e:  # noqa: BLE001
                problem(plot, "Coordinates out of range", str(e))
                continue
            box = g.boundingBox()
            if abs(box.yMinimum()) > 90 or abs(box.yMaximum()) > 90 or abs(box.xMinimum()) > 180 \
                    or abs(box.xMaximum()) > 180:
                if fix and abs(box.xMinimum()) <= 90 and abs(box.xMaximum()) <= 90:
                    g = QgsGeometry(g.constGet().clone())
                    g.get().swapXy()
                    problem(plot, "Latitude and longitude swapped", tr("Latitude and longitude were swapped back."),
                            g.centroid().asPoint(), True, "fixed")
                else:
                    problem(plot, "Coordinates out of range",
                            tr("Coordinates outside ±90° / ±180°: not latitude/longitude."))
                    continue
            producer = str(f[f_prod]).strip() if f_prod and f[f_prod] not in (None, "") else ""
            cc = (str(f[f_cc]).strip().upper() if f_cc and f[f_cc] not in (None, "") else cc_all)
            com = (str(f[f_com]).strip().lower() if f_com and f[f_com] not in (None, "") else com_all)
            where = g.pointOnSurface().asPoint() if not g.isEmpty() else None
            is_point = g.type() == Qgis.GeometryType.Point
            if not is_point:
                if not g.isGeosValid():
                    if fix:
                        g2 = g.makeValid()
                        polys = [p for p in g2.asGeometryCollection() if p.type() == Qgis.GeometryType.Polygon] \
                            if g2.isMultipart() or g2.wkbType() == Qgis.WkbType.GeometryCollection else [g2]
                        g = QgsGeometry.collectGeometry(polys) if polys else g2
                        problem(plot, "Self-intersecting or invalid polygon", tr("Repaired (made valid)."), where,
                                True, "fixed")
                    else:
                        problem(plot, "Self-intersecting or invalid polygon",
                                tr("The outline crosses itself or is otherwise invalid."), where)
                parts = g.asMultiPolygon() if g.isMultipart() else [g.asPolygon()]
                if any(len(r) < 4 for poly in parts for r in poly):
                    problem(plot, "Too few corners", tr("A ring needs at least three corners."), where)
                area_ha = da.measureArea(g) / 10000.0
                if area_ha <= 0:
                    problem(plot, "Plot area is zero", tr("The polygon has no area."), where)
            else:
                declared = None
                if f_area and f[f_area] not in (None, ""):
                    try:
                        declared = float(str(f[f_area]).replace(",", "."))
                    except ValueError:
                        declared = None
                area_ha = declared or 0.0
                if declared is not None and declared > POLYGON_HA:
                    problem(plot, "Plot over 4 ha given as a point",
                            tr("Declared {a} ha: plots over 4 ha need a polygon.").format(a=f"{declared:g}"), where)
                if com == "cattle":
                    problem(plot, "Cattle plot given as a point",
                            tr("Cattle establishments are described by a polygon of the grazing land."), where,
                            severity="warning")
            if not producer:
                problem(plot, "Missing producer", tr("The producer's name is required."), where)
            if not re.fullmatch(r"[A-Z]{2}", cc or ""):
                problem(plot, "Country is not an ISO code",
                        tr("Use the two-letter ISO code (BR, CI, VN…), not '{c}'.").format(c=cc), where)
            elif countries and cc in countries and where is not None:
                cg = countries[cc]
                here = QgsGeometry.fromPointXY(QgsPointXY(where))
                if not cg.contains(here) and cg.distance(here) > 0.05:
                    flipped = QgsGeometry.fromPointXY(QgsPointXY(where.y(), where.x()))
                    if fix and cg.contains(flipped):
                        # within ±90° both ways: only the country tells that latitude and longitude were swapped
                        g = QgsGeometry(g.constGet().clone())
                        g.get().swapXy()
                        where = g.pointOnSurface().asPoint()
                        problem(plot, "Latitude and longitude swapped",
                                tr("Latitude and longitude were swapped back."), where, True, "fixed")
                    else:
                        problem(plot, "Country does not match the plot location",
                                tr("The plot is not in {c}.").format(c=cc), where, severity="warning")
            if com not in COMMODITIES:
                problem(plot, "Unknown commodity",
                        tr("'{c}' is not an EUDR commodity ({all}).").format(c=com, all=", ".join(COMMODITIES)), where)
            plots.append({"plot": plot, "geom": g, "producer": producer, "country": cc, "commodity": com,
                          "area_ha": area_ha, "where": where})
            feedback.setProgress(70 * k / total)

        # overlaps between plots of the same producer
        index = QgsSpatialIndex()
        for n, p in enumerate(plots):
            if p["geom"].type() == Qgis.GeometryType.Polygon:
                f = QgsFeature(n)
                f.setGeometry(p["geom"])
                index.addFeature(f)
        for n, p in enumerate(plots):
            if p["geom"].type() != Qgis.GeometryType.Polygon:
                continue
            for m in index.intersects(p["geom"].boundingBox()):
                if m <= n or plots[m]["producer"] != p["producer"]:
                    continue
                inter = p["geom"].intersection(plots[m]["geom"])
                ov = da.measureArea(inter) if not inter.isEmpty() else 0
                if ov > 1.0:
                    problem(p["plot"], "Overlaps another plot of the same producer",
                            tr("Overlaps plot {o} by {a} m².").format(o=plots[m]["plot"], a=f"{ov:,.0f}"),
                            inter.pointOnSurface().asPoint())

        # outputs
        by_plot = {}
        for pr in problems:
            by_plot.setdefault(pr[0], []).append(pr)
        if psink is not None:
            for plot, rule, sev, detail, fixed, where in problems:
                pfeat = QgsFeature(pf)
                if where is not None:
                    pfeat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(where)))
                pfeat.setAttributes([plot, tr(rule), sev, detail, bool(fixed)])
                psink.addFeature(pfeat, QgsFeatureSink.Flag.FastInsert)
        errors = 0
        features = []
        for p in plots:
            errs = [x for x in by_plot.get(p["plot"], []) if x[2] == "error"]
            errors += 1 if errs else 0
            if osink is not None:
                ofeat = QgsFeature(of)
                og = QgsGeometry(p["geom"])
                og.convertToMultiType()
                ofeat.setGeometry(og)
                ofeat.setAttributes([p["plot"], p["producer"], p["country"], p["commodity"], round(p["area_ha"], 4),
                                     not errs, "; ".join(tr(x[1]) for x in by_plot.get(p["plot"], []))])
                osink.addFeature(ofeat, QgsFeatureSink.Flag.FastInsert)
            features.append({"type": "Feature",
                             "properties": {"ProducerName": p["producer"], "ProducerCountry": p["country"],
                                            "ProductionPlace": p["plot"], "Area": round(p["area_ha"], 4),
                                            "Commodity": p["commodity"]},
                             "geometry": geojson_geometry(p["geom"])})
        results = {self.PROBLEMS: pid, "PLOTS": len(plots), "PLOTS_WITH_ERRORS": errors,
                   "PROBLEM_COUNT": len(problems)}
        if oid is not None:
            results[self.OUTPUT] = oid
        out = self.parameterAsFileOutput(parameters, self.GEOJSON, context)
        if out:
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump({"type": "FeatureCollection", "features": features}, fh, ensure_ascii=False,
                          separators=(",", ":"))
            mb = os.path.getsize(out) / 1e6
            if mb > MAX_FILE_MB:
                feedback.reportError(tr("The GeoJSON is {s} MB: split it into files under {m} MB for upload.").format(
                    s=f"{mb:.1f}", m=MAX_FILE_MB))
            results[self.GEOJSON] = out
        feedback.pushInfo(tr("{n} plots checked: {e} with errors, {p} problems listed.").format(
            n=len(plots), e=errors, p=len(problems)))
        return results
