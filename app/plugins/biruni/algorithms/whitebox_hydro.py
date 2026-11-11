"""Water on the terrain, on WhiteboxTools (the free open core, shipped beside the engine).

Three one-click tools the GRASS-based "Streams and watersheds from a DEM" does not give:
  - the watershed above chosen outlet points (a culvert, a dam site, a road crossing),
  - the spots where rain water ponds (street flooding, low plots), with depth, area and volume,
  - the land along the wadis that floods first (height above the nearest stream, HAND).
The DEM is conditioned by breaching (least-cost cuts through roads and embankments) before filling what is left,
which keeps valleys in their place better than filling alone.
"""
import math
import os
import tempfile

from qgis.core import (Qgis, QgsDistanceArea, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry,
                       QgsProcessingException, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterNumber,
                       QgsProcessingParameterRasterDestination, QgsProcessingParameterRasterLayer, QgsRectangle,
                       QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from .. import whitebox as wbt
from ..i18n import tr
from .base import BiruniAlgorithm


def _why(e):
    s = str(e)
    if s == "missing":
        return tr("WhiteboxTools, the terrain engine, is not installed with the program.")
    if s == "cancelled":
        return tr("Cancelled.")
    return tr("WhiteboxTools failed: {e}").format(e=s[-600:])


class _WhiteboxAlgorithm(BiruniAlgorithm):
    GROUP = "Hydrology"
    GROUP_ID = "hydrology"

    def _start(self, dem, context, feedback):
        from osgeo import gdal

        if not wbt.available():
            raise QgsProcessingException(_why("missing"))
        if dem is None:
            raise QgsProcessingException(tr("Choose the elevation (DEM)."))
        wd = tempfile.mkdtemp(prefix="biruni_wbt_")
        gdal.UseExceptions()
        src = gdal.Open(dem.source())
        if src is None:
            raise QgsProcessingException(tr("Could not read {p}").format(p=dem.source()))
        nd = src.GetRasterBand(1).GetNoDataValue()
        gdal.Translate(os.path.join(wd, "dem.tif"), src, format="GTiff", outputType=gdal.GDT_Float32,
                       bandList=[1], noData=nd if nd is not None else -32768.0)
        src = None
        crs = dem.crs()
        da = QgsDistanceArea()
        da.setSourceCrs(crs, context.transformContext())
        ell = context.ellipsoid()
        da.setEllipsoid(ell if ell and ell.upper() != "NONE" else (crs.ellipsoidAcronym() or "EPSG:7030"))
        if crs.isGeographic():
            feedback.pushWarning(tr("The DEM is in degrees: a DEM in metres (projected) gives better results."))
        ext = dem.extent()
        cx, cy = ext.center().x(), ext.center().y()
        dx, dy = dem.rasterUnitsPerPixelX(), dem.rasterUnitsPerPixelY()
        self.cell_m2 = da.measureArea(QgsGeometry.fromRect(QgsRectangle(cx, cy, cx + dx, cy + dy)))
        self.cell_m = math.sqrt(self.cell_m2) if self.cell_m2 > 0 else 0
        if self.cell_m2 <= 0:
            raise QgsProcessingException(tr("The DEM has no cell size."))
        self.wd, self.crs, self.da, self.dem = wd, crs, da, dem
        return wd

    def _wbt(self, tool, feedback, span, **args):
        if feedback.isCanceled():
            raise QgsProcessingException(_why("cancelled"))
        try:
            return wbt.run(tool, self.wd, feedback, span, **args)
        except wbt.WhiteboxError as e:
            raise QgsProcessingException(_why(e))

    def _condition(self, feedback, span=(0, 30)):
        """dem.tif -> breached.tif (least-cost breaching, the rest filled), d8.tif, acc.tif (cells)."""
        a, b = span
        m = a + (b - a) * 0.6
        feedback.pushInfo(tr("Breaching the sinks of the DEM…"))
        self._wbt("BreachDepressionsLeastCost", feedback, (a, m), dem="dem.tif", output="breached.tif",
                  dist=max(10, int(round(500 / max(self.cell_m, 0.01)))), fill=True)
        feedback.pushInfo(tr("Flow direction and accumulation…"))
        self._wbt("D8Pointer", feedback, (m, m + (b - m) / 2), dem="breached.tif", output="d8.tif")
        self._wbt("D8FlowAccumulation", feedback, (m + (b - m) / 2, b), input="d8.tif", pntr=True,
                  output="acc.tif", out_type="cells")

    def _read(self, name):
        from osgeo import gdal

        ds = gdal.Open(os.path.join(self.wd, name))
        band = ds.GetRasterBand(1)
        arr = band.ReadAsArray().astype("float64")
        nd = band.GetNoDataValue()
        return arr, nd, ds.GetGeoTransform(), ds.GetProjection()

    def _regions(self, labels, gt, wkt):
        """{label: QgsGeometry} of a raster of region numbers (0 = none)."""
        from osgeo import gdal, ogr

        h, w = labels.shape
        mem = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Int32)
        mem.SetGeoTransform(gt)
        mem.SetProjection(wkt)
        band = mem.GetRasterBand(1)
        band.WriteArray(labels.astype("int32"))
        mask = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
        mask.GetRasterBand(1).WriteArray((labels > 0).astype("uint8"))
        ods = (ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")).CreateDataSource("r")
        olyr = ods.CreateLayer("r", geom_type=ogr.wkbPolygon)
        olyr.CreateField(ogr.FieldDefn("DN", ogr.OFTInteger))
        gdal.Polygonize(band, mask.GetRasterBand(1), olyr, 0, ["8CONNECTED=8"])
        parts = {}
        for f in olyr:
            g = QgsGeometry()
            g.fromWkb(bytes(f.GetGeometryRef().ExportToWkb()))
            parts.setdefault(f.GetField(0), []).append(g)
        out = {}
        for k, gs in parts.items():
            g = gs[0] if len(gs) == 1 else QgsGeometry.unaryUnion(gs)
            g.convertToMultiType()
            out[k] = g
        return out


# ---- the watershed above outlet points --------------------------------------------------------------------------
class WatershedAtPoints(_WhiteboxAlgorithm):
    ID = "wbtwatershed"
    NAME = "Watershed above points"
    HELP = ("The area that drains to each chosen point - a culvert, a bridge, a dam site, the place a wadi crosses "
            "a road - with its area. Each point is first moved to the strongest flow within the snapping distance, "
            "so a point clicked next to the stream still finds it. Runs on WhiteboxTools (free, open source).")
    INPUT, POINTS, SNAP, OUTPUT, SNAPPED = "INPUT", "POINTS", "SNAP", "OUTPUT", "SNAPPED"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, tr("Elevation (DEM)")))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.POINTS, tr("Outlet points"), [Qgis.ProcessingSourceType.VectorPoint]))
        self.addParameter(QgsProcessingParameterNumber(
            self.SNAP, tr("Move each point to the stream within (m)"), Qgis.ProcessingNumberParameterType.Double,
            50.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Watersheds"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.SNAPPED, tr("Outlets on the stream"), Qgis.ProcessingSourceType.VectorPoint, optional=True,
            createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from osgeo import ogr, osr
        from qgis.core import QgsCoordinateTransform

        dem = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        src = self.parameterAsSource(parameters, self.POINTS, context)
        if src is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.POINTS))
        wd = self._start(dem, context, feedback)
        snap = self.parameterAsDouble(parameters, self.SNAP, context)
        if self.crs.isGeographic():
            snap /= 111320.0
        to_dem = QgsCoordinateTransform(src.sourceCrs(), self.crs, context.transformContext()) \
            if src.sourceCrs() != self.crs else None
        pts = []
        for f in src.getFeatures():
            g = QgsGeometry(f.geometry())
            if g.isNull() or g.isEmpty():
                continue
            if to_dem is not None:
                g.transform(to_dem)
            for p in (g.asMultiPoint() if g.isMultipart() else [g.asPoint()]):
                pts.append((f.id(), p))
        if not pts:
            raise QgsProcessingException(tr("There are no outlet points."))
        drv = ogr.GetDriverByName("ESRI Shapefile")
        ds = drv.CreateDataSource(os.path.join(wd, "pour.shp"))
        sr = osr.SpatialReference()
        sr.ImportFromWkt(self.crs.toWkt(Qgis.CrsWktVariant.Preferred))
        lyr = ds.CreateLayer("pour", sr, ogr.wkbPoint)
        lyr.CreateField(ogr.FieldDefn("OUTLET", ogr.OFTInteger))
        for n, (_fid, p) in enumerate(pts, 1):
            of = ogr.Feature(lyr.GetLayerDefn())
            of.SetField("OUTLET", n)
            g = ogr.Geometry(ogr.wkbPoint)
            g.AddPoint_2D(p.x(), p.y())
            of.SetGeometry(g)
            lyr.CreateFeature(of)
        ds = None

        self._condition(feedback, (0, 50))
        feedback.pushInfo(tr("Moving the points to the stream…"))
        self._wbt("SnapPourPoints", feedback, (50, 60), pour_pts="pour.shp", flow_accum="acc.tif",
                  output="snapped.shp", snap_dist=snap)
        feedback.pushInfo(tr("Tracing the watersheds…"))
        self._wbt("Watershed", feedback, (60, 85), d8_pntr="d8.tif", pour_pts="snapped.shp", output="ws.tif")
        arr, nd, gt, wkt = self._read("ws.tif")
        import numpy as np

        lab = np.where((arr > 0) & (arr != (nd if nd is not None else -32768)), arr, 0).astype("int64")
        regions = self._regions(lab, gt, wkt)

        fields = QgsFields()
        fields.append(QgsField("outlet", QMetaType.Type.Int))
        fields.append(QgsField("point_fid", QMetaType.Type.LongLong))
        fields.append(QgsField("area_km2", QMetaType.Type.Double))
        fields.append(QgsField("area_ha", QMetaType.Type.Double))
        sink, sid = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.MultiPolygon,
                                         self.crs)
        snapped = QgsVectorLayer(os.path.join(wd, "snapped.shp"), "snapped", "ogr")
        order = {}
        for n, f in enumerate(snapped.getFeatures(), 1):
            order[n] = f
        for k in sorted(regions):
            g = regions[k]
            area = self.da.measureArea(g)
            n = int(k)
            fid = pts[n - 1][0] if 0 < n <= len(pts) else None
            feat = QgsFeature(fields)
            feat.setGeometry(g)
            feat.setAttributes([n, fid, round(area / 1e6, 4), round(area / 1e4, 2)])
            sink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
        out = {self.OUTPUT: sid}
        pf = QgsFields()
        pf.append(QgsField("outlet", QMetaType.Type.Int))
        psink, pid = self.parameterAsSink(parameters, self.SNAPPED, context, pf, Qgis.WkbType.Point, self.crs)
        if psink is not None:
            for n, f in order.items():
                feat = QgsFeature(pf)
                feat.setGeometry(f.geometry())
                feat.setAttributes([n])
                psink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
            out[self.SNAPPED] = pid
        missing = len(pts) - len(regions)
        if missing > 0:
            feedback.pushWarning(tr("{n} points drain no area (on the edge of the DEM, or outside it).").format(
                n=missing))
        feedback.setProgress(100)
        return out


# ---- where water ponds ---------------------------------------------------------------------------------------
class PondingSpots(_WhiteboxAlgorithm):
    ID = "wbtponding"
    NAME = "Where rain water ponds"
    HELP = ("The low spots of the terrain where rain water collects and has no way out: streets and plots that "
            "flood in a storm, the places a drain is needed. Each spot gets its deepest point, its area and the "
            "volume of water it holds when full. Spots shallower or smaller than the minimums are left out. Runs on "
            "WhiteboxTools (free, open source).")
    INPUT, MIN_DEPTH, MIN_AREA, OUTPUT, DEPTH = "INPUT", "MIN_DEPTH", "MIN_AREA", "OUTPUT", "DEPTH"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, tr("Elevation (DEM)")))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_DEPTH, tr("Minimum depth (m)"), Qgis.ProcessingNumberParameterType.Double, 0.2, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_AREA, tr("Minimum area (m²)"), Qgis.ProcessingNumberParameterType.Double, 50.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Ponding spots"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.DEPTH, tr("Water depth"), optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        import numpy as np
        from scipy import ndimage

        dem = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        self._start(dem, context, feedback)
        min_depth = self.parameterAsDouble(parameters, self.MIN_DEPTH, context)
        min_area = self.parameterAsDouble(parameters, self.MIN_AREA, context)
        feedback.pushInfo(tr("Filling the sinks to find how deep each one is…"))
        self._wbt("DepthInSink", feedback, (0, 60), dem="dem.tif", output="depth.tif", zero_background=True)
        depth, nd, gt, wkt = self._read("depth.tif")
        if nd is not None:
            depth[depth == nd] = 0
        depth[~np.isfinite(depth)] = 0
        wet = depth > 0
        lab, n = ndimage.label(wet, structure=np.ones((3, 3)))
        feedback.setProgress(70)
        idx = np.arange(1, n + 1)
        cells = ndimage.sum(np.ones_like(depth), lab, idx) if n else []
        deepest = ndimage.maximum(depth, lab, idx) if n else []
        vol = ndimage.sum(depth, lab, idx) if n else []
        keep = {int(i) for i, c, d in zip(idx, cells, deepest) if d >= min_depth and c * self.cell_m2 >= min_area}
        lab = np.where(np.isin(lab, list(keep)), lab, 0) if keep else np.zeros_like(lab)
        regions = self._regions(lab, gt, wkt) if keep else {}
        feedback.setProgress(90)

        fields = QgsFields()
        for name, t in (("spot", QMetaType.Type.Int), ("max_depth_m", QMetaType.Type.Double),
                        ("area_m2", QMetaType.Type.Double), ("volume_m3", QMetaType.Type.Double)):
            fields.append(QgsField(name, t))
        sink, sid = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.MultiPolygon,
                                         self.crs)
        stats = {int(i): (float(d), float(c), float(v)) for i, c, d, v in zip(idx, cells, deepest, vol)}
        spots = sorted(regions, key=lambda k: -stats[k][2])
        for n_, k in enumerate(spots, 1):
            d, c, v = stats[k]
            feat = QgsFeature(fields)
            feat.setGeometry(regions[k])
            feat.setAttributes([n_, round(d, 2), round(c * self.cell_m2, 1), round(v * self.cell_m2, 1)])
            sink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
        out = {self.OUTPUT: sid}
        dest = self.parameterAsOutputLayer(parameters, self.DEPTH, context)
        if dest:
            from osgeo import gdal

            gdal.Translate(dest, os.path.join(self.wd, "depth.tif"))
            out[self.DEPTH] = dest
        feedback.pushInfo(tr("{n} ponding spots, holding {v} m³ of water when full.").format(
            n=len(spots), v=round(sum(stats[k][2] for k in spots) * self.cell_m2)))
        feedback.setProgress(100)
        return out


# ---- the land along the wadis that floods first --------------------------------------------------------------
class FloodZoneAlongStreams(_WhiteboxAlgorithm):
    ID = "wbtfloodzone"
    NAME = "Flood-prone land along wadis (height above stream)"
    HELP = ("The land that floods first when a wadi or stream rises: every place whose height above the nearest "
            "stream it drains to (HAND) is under the chosen height. Also gives the stream network with its Strahler "
            "order. A first screening for building permits and plot planning near wadis, not a hydraulic flood "
            "model. Runs on WhiteboxTools (free, open source).")
    INPUT, MIN_AREA, HEIGHT, ZONE, STREAMS, HAND = "INPUT", "MIN_AREA", "HEIGHT", "ZONE", "STREAMS", "HAND"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, tr("Elevation (DEM)")))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_AREA, tr("Minimum drainage area of a stream (km², 0 = automatic)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.HEIGHT, tr("Flood height above the stream (m)"), Qgis.ProcessingNumberParameterType.Double, 2.0,
            minValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.ZONE, tr("Flood-prone land"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.STREAMS, tr("Streams"), Qgis.ProcessingSourceType.VectorLine))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.HAND, tr("Height above the stream"), optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        import numpy as np
        from scipy import ndimage

        dem = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        self._start(dem, context, feedback)
        min_area = self.parameterAsDouble(parameters, self.MIN_AREA, context)
        height = self.parameterAsDouble(parameters, self.HEIGHT, context)
        cells = dem.width() * dem.height()
        threshold = round(min_area * 1e6 / self.cell_m2) if min_area > 0 else round(cells * 0.005)
        threshold = max(int(threshold), 10)
        feedback.pushInfo(tr("A stream drains at least {n} cells ({a:.3f} km²).").format(
            n=threshold, a=threshold * self.cell_m2 / 1e6))
        self._condition(feedback, (0, 40))
        self._wbt("ExtractStreams", feedback, (40, 45), flow_accum="acc.tif", output="streams.tif",
                  threshold=threshold, zero_background=True)
        self._wbt("StrahlerStreamOrder", feedback, (45, 55), d8_pntr="d8.tif", streams="streams.tif",
                  output="order.tif", zero_background=True)
        self._wbt("RasterStreamsToVector", feedback, (55, 65), streams="order.tif", d8_pntr="d8.tif",
                  output="streams.shp")
        feedback.pushInfo(tr("Height of every place above its stream…"))
        self._wbt("ElevationAboveStream", feedback, (65, 85), dem="breached.tif", streams="streams.tif",
                  output="hand.tif")

        hand, nd, gt, wkt = self._read("hand.tif")
        ok = np.isfinite(hand) & (hand >= 0) & (hand <= height)
        if nd is not None:
            ok &= hand != nd
        lab, n = ndimage.label(ok, structure=np.ones((3, 3)))
        regions = self._regions(lab, gt, wkt) if n else {}
        fields = QgsFields()
        fields.append(QgsField("zone", QMetaType.Type.Int))
        fields.append(QgsField("area_ha", QMetaType.Type.Double))
        fields.append(QgsField("height_m", QMetaType.Type.Double))
        sink, sid = self.parameterAsSink(parameters, self.ZONE, context, fields, Qgis.WkbType.MultiPolygon, self.crs)
        min_piece = self.cell_m2 * 4
        n_ = 0
        for k in sorted(regions):
            g = regions[k]
            area = self.da.measureArea(g)
            if area < min_piece:
                continue
            n_ += 1
            feat = QgsFeature(fields)
            feat.setGeometry(g)
            feat.setAttributes([n_, round(area / 1e4, 2), height])
            sink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)

        sf = QgsFields()
        sf.append(QgsField("stream_id", QMetaType.Type.Int))
        sf.append(QgsField("strahler", QMetaType.Type.Int))
        sf.append(QgsField("length_m", QMetaType.Type.Double))
        ssink, ssid = self.parameterAsSink(parameters, self.STREAMS, context, sf, Qgis.WkbType.LineString, self.crs)
        sl = QgsVectorLayer(os.path.join(self.wd, "streams.shp"), "streams", "ogr")
        names = [f.name() for f in sl.fields()]
        val = "STRM_VAL" if "STRM_VAL" in names else (names[-1] if names else None)
        for m, f in enumerate(sl.getFeatures(), 1):
            g = f.geometry()
            if g.isNull() or g.isEmpty():
                continue
            for part in g.asGeometryCollection():
                if QgsWkbTypes.geometryType(part.wkbType()) != Qgis.GeometryType.Line:
                    continue
                feat = QgsFeature(sf)
                feat.setGeometry(part)
                feat.setAttributes([m, int(f[val]) if val and f[val] is not None else None,
                                    round(self.da.measureLength(part), 2)])
                ssink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
        out = {self.ZONE: sid, self.STREAMS: ssid}
        dest = self.parameterAsOutputLayer(parameters, self.HAND, context)
        if dest:
            from osgeo import gdal

            gdal.Translate(dest, os.path.join(self.wd, "hand.tif"))
            out[self.HAND] = dest
        feedback.pushInfo(tr("{n} pieces of flood-prone land under {h} m above their stream.").format(n=n_, h=height))
        feedback.setProgress(100)
        return out
