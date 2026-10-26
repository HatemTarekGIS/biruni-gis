from qgis.core import (Qgis, QgsDistanceArea, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry,
                       QgsProcessingException, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterNumber, QgsProcessingParameterRasterDestination,
                       QgsProcessingParameterRasterLayer, QgsRectangle, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

LINE, AREA = 2, 3  # GRASS_OUTPUT_TYPE_PARAMETER: auto, point, line, area


def strahler(lines):
    """Strahler order of a stream network. lines: {id: (start, end)} with the
    lines pointing downstream; a line's upstream lines are those ending where it
    starts. Iterative, so deep networks don't hit the recursion limit."""
    ending_at = {}
    for k, (_s, e) in lines.items():
        ending_at.setdefault(e, []).append(k)
    order = {}
    for root in lines:
        stack = [root]
        while stack:
            k = stack[-1]
            if k in order:
                stack.pop()
                continue
            ups = [u for u in ending_at.get(lines[k][0], []) if u != k]
            todo = [u for u in ups if u not in order]
            if todo:
                stack.extend(todo)
                continue
            stack.pop()
            if not ups:
                order[k] = 1
            else:
                top = max(order[u] for u in ups)
                order[k] = top + 1 if sum(1 for u in ups if order[u] == top) > 1 else top
    return order


def _node(p):
    return round(p.x(), 6), round(p.y(), 6)


class StreamsAndBasins(BiruniAlgorithm):
    """One-click drainage analysis from a DEM, on GRASS (bundled)."""

    ID = "streamsbasins"
    NAME = "Streams and watersheds from a DEM"
    GROUP = "Hydrology"
    GROUP_ID = "hydrology"
    HELP = ("Finds where water flows on a terrain model in one step: fills the sinks (pits) of the DEM, computes "
            "flow direction and flow accumulation, then draws the stream network with its Strahler order and the "
            "sub-watershed of every stream with its area. The minimum drainage area decides how small a valley "
            "still counts as a stream (0 = chosen from the size of the DEM). Works best on a DEM in metres in a "
            "projected coordinate system, such as the one from 'Download Elevation (DEM 30 m)'. Runs on GRASS GIS.")

    INPUT, MIN_AREA = "INPUT", "MIN_AREA"
    STREAMS, BASINS, FILLED, ACCUMULATION = "STREAMS", "BASINS", "FILLED", "ACCUMULATION"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, tr("Elevation (DEM)")))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_AREA, tr("Minimum drainage area of a stream (km², 0 = automatic)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.STREAMS, tr("Streams"), Qgis.ProcessingSourceType.VectorLine))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.BASINS, tr("Watersheds"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.ACCUMULATION, tr("Flow accumulation"), optional=True, createByDefault=True))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.FILLED, tr("Filled DEM (no sinks)"), optional=True, createByDefault=False))

    def _run(self, alg_id, params, context, feedback):
        import processing

        try:
            return processing.run(alg_id, params, context=context, feedback=feedback, is_child_algorithm=True)
        except QgsProcessingException as e:
            if "GRASS" in str(e):
                raise QgsProcessingException(tr("GRASS GIS could not run: {e}").format(e=e))
            raise

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from qgis.core import QgsApplication

        dem = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if dem is None:
            raise QgsProcessingException(self.invalidRasterError(parameters, self.INPUT))
        if QgsApplication.processingRegistry().algorithmById("grass:r.watershed") is None:
            raise QgsProcessingException(tr("The GRASS GIS tools are not available."))
        crs = dem.crs()
        da = QgsDistanceArea()
        da.setSourceCrs(crs, context.transformContext())
        ell = context.ellipsoid()
        da.setEllipsoid(ell if ell and ell.upper() != "NONE" else (crs.ellipsoidAcronym() or "EPSG:7030"))
        if crs.isGeographic():
            feedback.pushWarning(tr("The DEM is in degrees: areas are measured on the ellipsoid, but a DEM in "
                                    "metres (projected) gives better flow directions."))

        # the drainage threshold in cells, from km² and the true size of a cell in the middle of the DEM
        ext = dem.extent()
        cx, cy = ext.center().x(), ext.center().y()
        dx, dy = dem.rasterUnitsPerPixelX(), dem.rasterUnitsPerPixelY()
        cell = QgsGeometry.fromRect(QgsRectangle(cx, cy, cx + dx, cy + dy))
        cell_km2 = da.measureArea(cell) / 1e6
        if cell_km2 <= 0:
            raise QgsProcessingException(tr("The DEM has no cell size."))
        min_area = self.parameterAsDouble(parameters, self.MIN_AREA, context)
        cells = dem.width() * dem.height()
        threshold = round(min_area / cell_km2) if min_area > 0 else round(cells * 0.005)
        threshold = max(threshold, 10)
        feedback.pushInfo(tr("A stream drains at least {n} cells ({a:.3f} km²).").format(
            n=threshold, a=threshold * cell_km2))

        results = {}
        filled = self.parameterAsOutputLayer(parameters, self.FILLED, context)
        if filled:
            feedback.pushInfo(tr("Filling sinks…"))
            self._run("grass:r.fill.dir", {"input": dem.source(), "format": 0, "output": filled,
                                           "direction": "TEMPORARY_OUTPUT", "areas": "TEMPORARY_OUTPUT"},
                      context, feedback)
            results[self.FILLED] = filled
        feedback.setProgress(15)
        if feedback.isCanceled():
            return {}

        feedback.pushInfo(tr("Flow direction and accumulation…"))
        acc = self.parameterAsOutputLayer(parameters, self.ACCUMULATION, context) or "TEMPORARY_OUTPUT"
        ws = self._run("grass:r.watershed", {"elevation": dem.source(), "threshold": threshold, "-s": True,
                                             "-a": True, "accumulation": acc, "basin": "TEMPORARY_OUTPUT"},
                       context, feedback)
        if self.parameterAsOutputLayer(parameters, self.ACCUMULATION, context):
            results[self.ACCUMULATION] = ws["accumulation"]
        feedback.setProgress(45)
        if feedback.isCanceled():
            return {}

        feedback.pushInfo(tr("Tracing the streams…"))
        st = self._run("grass:r.stream.extract", {"elevation": dem.source(), "accumulation": ws["accumulation"],
                                                  "threshold": threshold, "stream_vector": "TEMPORARY_OUTPUT",
                                                  "GRASS_OUTPUT_TYPE_PARAMETER": LINE}, context, feedback)
        feedback.setProgress(65)
        bs = self._run("grass:r.to.vect", {"input": ws["basin"], "type": 2, "column": "basin", "-v": True,
                                           "-s": True, "output": "TEMPORARY_OUTPUT",
                                           "GRASS_OUTPUT_TYPE_PARAMETER": AREA}, context, feedback)
        feedback.setProgress(80)

        # streams: Strahler order + true length
        src = QgsVectorLayer(st["stream_vector"], "streams", "ogr")
        geoms, ends = {}, {}
        for f in src.getFeatures():
            g = f.geometry()
            if g.isNull() or g.isEmpty():
                continue
            parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
            pts = [p for part in parts for p in part]
            if len(pts) < 2:
                continue
            geoms[f.id()] = g
            ends[f.id()] = (_node(pts[0]), _node(pts[-1]))
        order = strahler(ends)
        sf = QgsFields()
        sf.append(QgsField("stream_id", QMetaType.Type.Int))
        sf.append(QgsField("strahler", QMetaType.Type.Int))
        sf.append(QgsField("length_m", QMetaType.Type.Double))
        ssink, sid = self.parameterAsSink(parameters, self.STREAMS, context, sf, Qgis.WkbType.LineString, crs)
        for n, k in enumerate(sorted(geoms), 1):
            for part in geoms[k].asGeometryCollection():
                feat = QgsFeature(sf)
                feat.setGeometry(part)
                feat.setAttributes([n, order[k], round(da.measureLength(geoms[k]), 2)])
                ssink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)

        # watersheds: one polygon per basin (GRASS may split one into several rings)
        src = QgsVectorLayer(bs["output"], "basins", "ogr")
        by_basin = {}
        for f in src.getFeatures():
            g = f.geometry()
            if not g.isNull() and not g.isEmpty():
                by_basin.setdefault(f["cat"], []).append(g)
        bf = QgsFields()
        bf.append(QgsField("basin_id", QMetaType.Type.Int))
        bf.append(QgsField("area_km2", QMetaType.Type.Double))
        bsink, bid = self.parameterAsSink(parameters, self.BASINS, context, bf, Qgis.WkbType.MultiPolygon, crs)
        min_basin = threshold * cell_km2 * 1e6 * 0.05  # drop the edge crumbs GRASS leaves
        n = 0
        for _cat, gs in sorted(by_basin.items()):
            g = QgsGeometry.unaryUnion(gs)
            area = da.measureArea(g)
            if area < min_basin:
                continue
            g.convertToMultiType()
            n += 1
            feat = QgsFeature(bf)
            feat.setGeometry(g)
            feat.setAttributes([n, round(area / 1e6, 4)])
            bsink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
        feedback.setProgress(100)
        feedback.pushInfo(tr("{s} streams (highest order {o}) and {b} watersheds.").format(
            s=len(geoms), o=max(order.values(), default=0), b=n))
        results.update({self.STREAMS: sid, self.BASINS: bid, "STREAM_COUNT": len(geoms), "BASIN_COUNT": n,
                        "THRESHOLD_CELLS": threshold})
        return results
