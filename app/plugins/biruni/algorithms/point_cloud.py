"""Point clouds and drones: a terrain model from LiDAR / photogrammetry points in one step, elevation
profiles and stockpile volumes. Runs PDAL (bundled with the engine) with plain JSON pipelines."""
import json
import math
import os
import shutil
import subprocess
import tempfile

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea, QgsFeature,
                       QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessingException,
                       QgsProcessingParameterBoolean, QgsProcessingParameterCrs, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterPointCloudDestination, QgsProcessingParameterPointCloudLayer,
                       QgsProcessingParameterRasterDestination, QgsProcessingParameterRasterLayer,
                       QgsProcessingParameterVectorDestination, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

NODATA = -9999.0
NICE_STEPS = (0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100)


def pdal_exe():
    """The PDAL command line of the engine (engine/bin), or one on PATH."""
    from qgis.core import QgsApplication

    exe = os.path.normpath(os.path.join(QgsApplication.prefixPath(), "..", "..", "bin", "pdal.exe"))
    if os.path.exists(exe):
        return exe
    found = shutil.which("pdal")
    if not found:
        raise QgsProcessingException(tr("PDAL, the point cloud engine, was not found."))
    return found


def run_pdal(args, feedback=None):
    """Run pdal with arguments; returns stdout. Cancelling stops the process."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen([pdal_exe(), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            creationflags=flags)
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if feedback is not None and feedback.isCanceled():
                proc.kill()
                proc.communicate()
                return None
    if proc.returncode != 0:
        raise QgsProcessingException(tr("PDAL failed: {e}").format(e=(err or out).strip()[-600:]))
    return out


def run_pipeline(stages, feedback=None, metadata=False):
    """Run a PDAL pipeline (list of stages); returns its metadata when asked."""
    work = tempfile.mkdtemp(prefix="biruni_pdal_")
    path = os.path.join(work, "pipeline.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pipeline": stages}, f)
    args = ["pipeline", path]
    meta = os.path.join(work, "metadata.json")
    if metadata:
        args += ["--metadata", meta]
    if run_pdal(args, feedback) is None:
        return None
    if metadata:
        with open(meta, encoding="utf-8") as f:
            return json.load(f)
    return {}


def summary(path, feedback=None):
    """Point count, bounds and coordinate system (WKT, '' when none) of a point cloud file."""
    out = run_pdal(["info", "--summary", path], feedback)
    s = json.loads(out)["summary"]
    srs = s.get("srs") or {}
    return {"count": int(s.get("num_points", 0)), "bounds": s["bounds"], "wkt": srs.get("wkt") or "",
            "geographic": bool(srs.get("isgeographic"))}


def class_counts(path, feedback=None):
    """{classification code: number of points}."""
    meta = run_pipeline([path, {"type": "filters.stats", "dimensions": "Classification",
                                "count": "Classification"}], feedback, metadata=True)
    if meta is None:
        return {}
    stages = meta.get("stages", meta.get("metadata", {}))
    stats = stages.get("filters.stats", {})
    counts = {}
    for s in stats.get("statistic", []):
        if s.get("name") == "Classification":
            for item in s.get("counts", []):
                value, n = item.split("/")
                counts[int(float(value))] = int(n)
    return counts


def nice_step(value):
    """The smallest 'round' step ≥ value."""
    for s in NICE_STEPS:
        if s >= value:
            return s
    return NICE_STEPS[-1]


def auto_resolution(count, bounds):
    """Cell size from the mean point spacing: about two points per cell side, at least 10 cm."""
    area = max((bounds["maxx"] - bounds["minx"]) * (bounds["maxy"] - bounds["miny"]), 1e-9)
    spacing = math.sqrt(area / max(count, 1))
    return nice_step(max(2 * spacing, 0.1))


def grid(bounds, res):
    """Raster origin (lower left), width and height that cover the bounds on whole multiples of the cell size."""
    ox = math.floor(bounds["minx"] / res) * res
    oy = math.floor(bounds["miny"] / res) * res
    w = max(int(math.ceil((bounds["maxx"] - ox) / res)), 1)
    h = max(int(math.ceil((bounds["maxy"] - oy) / res)), 1)
    return {"origin_x": ox, "origin_y": oy, "width": w, "height": h}


def fill_gaps(path, max_distance=10):
    """Fill small empty cells of a raster from their neighbours (in place)."""
    from osgeo import gdal

    gdal.UseExceptions()
    ds = gdal.Open(path, gdal.GA_Update)
    band = ds.GetRasterBand(1)
    gdal.FillNodata(band, None, max_distance, 0)
    band.FlushCache()
    ds = None


def raster_difference(top, bottom, out, floor=None):
    """out = top - bottom on the same grid; empty where either is empty."""
    from osgeo import gdal

    gdal.UseExceptions()
    a = gdal.Open(top)
    b = gdal.Open(bottom)
    za = a.GetRasterBand(1).ReadAsArray().astype("float64")
    zb = b.GetRasterBand(1).ReadAsArray().astype("float64")
    na, nb = a.GetRasterBand(1).GetNoDataValue(), b.GetRasterBand(1).GetNoDataValue()
    diff = za - zb
    if floor is not None:
        diff[diff < floor] = floor
    empty = (za == na) | (zb == nb)
    diff[empty] = NODATA
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out, a.RasterXSize, a.RasterYSize, 1, gdal.GDT_Float32, ["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(a.GetGeoTransform())
    ds.SetProjection(a.GetProjection())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(diff)
    ds = None


def _source_path(layer):
    """File path of a point cloud layer (COPC/LAS/LAZ/EPT)."""
    path = layer.source().split("|")[0]
    if path.startswith("ept://"):
        path = path[6:]
    return path


class GroundFromPoints(BiruniAlgorithm):
    ID = "lasground"
    NAME = "Ground surface (DTM) from LiDAR / drone points"
    GROUP = "Point clouds & drones"
    GROUP_ID = "pointcloud"
    HELP = ("Makes a terrain model from a point cloud (LiDAR or drone photogrammetry: LAS, LAZ or COPC) in one "
            "step. The ground points are found automatically (buildings, trees, cars and noise are left out) - or "
            "the ground class already in the file is used. Outputs the bare-earth terrain (DTM), the surface with "
            "buildings and trees (DSM), their heights above the ground (nDSM), a hillshade, contour lines and a "
            "classified copy of the points. The cell size can be left at 0 to choose it from the point density. "
            "Works in the coordinate system of the points, which should be projected (metres); a file without one "
            "can be given its system here. Runs on PDAL.")

    INPUT, MODE, NOISE, RESOLUTION, CRS, INTERVAL = "INPUT", "MODE", "NOISE", "RESOLUTION", "CRS", "INTERVAL"
    DTM, DSM, NDSM, HILLSHADE, CONTOURS, CLASSIFIED = "DTM", "DSM", "NDSM", "HILLSHADE", "CONTOURS", "CLASSIFIED"
    MODES = ["Find the ground points (recommended)", "Use the ground class in the file (class 2)"]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterPointCloudLayer(self.INPUT, tr("Point cloud (LAS, LAZ, COPC)")))
        self.addParameter(QgsProcessingParameterEnum(
            self.MODE, tr("Ground points"), [tr(m) for m in self.MODES], defaultValue=0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.NOISE, tr("Remove noise (isolated points above or below the ground)"), defaultValue=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.RESOLUTION, tr("Cell size (m, 0 = from the point density)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, tr("Coordinate system, if the file has none"), optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.INTERVAL, tr("Contour interval (m, 0 = automatic)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterRasterDestination(self.DTM, tr("Terrain (DTM)")))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.DSM, tr("Surface with buildings and trees (DSM)"), optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.NDSM, tr("Heights above the ground (nDSM)"), optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.HILLSHADE, tr("Hillshade"), optional=True, createByDefault=True))
        self.addParameter(QgsProcessingParameterVectorDestination(
            self.CONTOURS, tr("Contour lines"), Qgis.ProcessingSourceType.VectorLine, optional=True,
            createByDefault=True))
        self.addParameter(QgsProcessingParameterPointCloudDestination(
            self.CLASSIFIED, tr("Classified points"), optional=True, createByDefault=False))

    def flags(self):
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        import processing

        layer = self.parameterAsPointCloudLayer(parameters, self.INPUT, context)
        if layer is None:
            raise QgsProcessingException(self.invalidPointCloudError(parameters, self.INPUT))
        src = _source_path(layer)
        if not os.path.exists(src):
            raise QgsProcessingException(tr("The point cloud file was not found: {p}").format(p=src))
        mode = self.parameterAsEnum(parameters, self.MODE, context)
        noise = self.parameterAsBool(parameters, self.NOISE, context)
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        work = tempfile.mkdtemp(prefix="biruni_ground_")

        feedback.pushInfo(tr("Reading the point cloud…"))
        info = summary(src, feedback)
        if feedback.isCanceled():
            return {}
        count, bounds = info["count"], info["bounds"]
        if count == 0:
            raise QgsProcessingException(tr("The point cloud has no points."))
        low = src.lower()
        kind = ("readers.copc" if low.endswith(".copc.laz") else "readers.ept" if low.endswith(".json")
                else "readers.las")
        reader = {"type": kind, "filename": src}
        srs = ""
        if not info["wkt"]:
            if crs.isValid():
                srs = crs.authid() or crs.toWkt()
                reader["override_srs"] = srs
            else:
                feedback.pushWarning(tr("The points have no coordinate system: choose one in the tool, or the "
                                        "outputs will have none."))
        elif info["geographic"]:
            raise QgsProcessingException(tr("The points are in degrees. Reproject them to a projected system "
                                            "(metres) first, e.g. with 'Reproject point cloud'."))
        area = max((bounds["maxx"] - bounds["minx"]) * (bounds["maxy"] - bounds["miny"]), 1e-9)
        density = count / area
        res = self.parameterAsDouble(parameters, self.RESOLUTION, context) or auto_resolution(count, bounds)
        g = grid(bounds, res)
        feedback.pushInfo(tr("{n} points, {d:.1f} points per m², cell size {r} m, {w} × {h} cells.").format(
            n=f"{count:,}", d=density, r=res, w=g["width"], h=g["height"]))
        feedback.setProgress(5)

        # ---- classify ----------------------------------------------------------------------------
        classified = self.parameterAsOutputLayer(parameters, self.CLASSIFIED, context)
        cls_path = os.path.join(work, "classified.laz")
        stages = [reader]
        keep = mode == 1
        if keep:
            counts = class_counts(src, feedback)
            if not counts.get(2):
                feedback.pushWarning(tr("The file has no ground class (2): the ground points are found instead."))
                keep = False
        if not keep:
            stages.append({"type": "filters.assign", "value": "Classification = 1"})
        if noise:
            stages += [{"type": "filters.outlier", "method": "statistical", "mean_k": 8, "multiplier": 3.0},
                       {"type": "filters.elm"}]
        if not keep:
            stages.append({"type": "filters.smrf", "ignore": "Classification[7:7]"})
        stages.append({"type": "writers.las", "filename": cls_path, "compression": "laszip", "forward": "all",
                       "minor_version": 4})
        feedback.pushInfo(tr("Finding the ground points…") if not keep else tr("Using the ground class…"))
        if run_pipeline(stages, feedback) is None:
            return {}
        feedback.setProgress(40)
        counts = class_counts(cls_path, feedback)
        ground = counts.get(2, 0)
        noise_n = counts.get(7, 0)
        ground_pct = 100.0 * ground / max(count, 1)
        feedback.pushInfo(tr("{g:.1f}% of the points are ground, {n} noise points left out.").format(
            g=ground_pct, n=f"{noise_n:,}"))
        if ground < 3:
            raise QgsProcessingException(tr("No ground points were found."))

        # ---- terrain (DTM): triangulated ground, so the ground under buildings is bridged ----------
        dtm = self.parameterAsOutputLayer(parameters, self.DTM, context)
        ground_stages = [cls_path, {"type": "filters.range", "limits": "Classification[2:2]"}]
        if ground > 3_000_000:  # thin very dense ground before triangulating: cells hold enough points anyway
            ground_stages.append({"type": "filters.sample", "radius": res / 2})
        ground_stages += [{"type": "filters.delaunay"},
                          {"type": "filters.faceraster", "resolution": res, **g},
                          {"type": "writers.raster", "filename": dtm, "data_type": "float32",
                           "gdalopts": "COMPRESS=DEFLATE,TILED=YES"}]
        feedback.pushInfo(tr("Building the terrain model…"))
        if run_pipeline(ground_stages, feedback) is None:
            return {}
        feedback.setProgress(65)
        results = {self.DTM: dtm, "POINT_COUNT": count, "DENSITY": density, "GROUND_PERCENT": ground_pct,
                   "RESOLUTION": res}

        # ---- surface (DSM) and heights (nDSM) --------------------------------------------------------
        dsm = self.parameterAsOutputLayer(parameters, self.DSM, context)
        ndsm = self.parameterAsOutputLayer(parameters, self.NDSM, context)
        if dsm or ndsm:
            dsm_path = dsm or os.path.join(work, "dsm.tif")
            if run_pipeline([cls_path, {"type": "filters.range", "limits": "Classification![7:7]"},
                             {"type": "writers.gdal", "filename": dsm_path, "resolution": res, "output_type": "max",
                              "data_type": "float32", "window_size": 3, "nodata": NODATA,
                              "gdalopts": "COMPRESS=DEFLATE,TILED=YES", **g}], feedback) is None:
                return {}
            fill_gaps(dsm_path)
            if dsm:
                results[self.DSM] = dsm
            if ndsm:
                raster_difference(dsm_path, dtm, ndsm, floor=0.0)
                results[self.NDSM] = ndsm
        feedback.setProgress(80)

        hillshade = self.parameterAsOutputLayer(parameters, self.HILLSHADE, context)
        if hillshade:
            processing.run("native:hillshade", {"INPUT": dtm, "Z_FACTOR": 1, "AZIMUTH": 315, "V_ANGLE": 45,
                                                "OUTPUT": hillshade},
                           context=context, feedback=feedback, is_child_algorithm=True)
            results[self.HILLSHADE] = hillshade

        contours = self.parameterAsOutputLayer(parameters, self.CONTOURS, context)
        if contours:
            interval = self.parameterAsDouble(parameters, self.INTERVAL, context)
            if not interval:
                interval = nice_step(max(bounds["maxz"] - bounds["minz"], 0.1) / 25)
            feedback.pushInfo(tr("Contours every {i} m.").format(i=interval))
            out = processing.run("gdal:contour", {"INPUT": dtm, "BAND": 1, "INTERVAL": interval,
                                                  "FIELD_NAME": "elev", "OUTPUT": contours},
                                 context=context, feedback=feedback, is_child_algorithm=True)
            results[self.CONTOURS] = out["OUTPUT"]
            results["CONTOUR_INTERVAL"] = interval

        if classified:
            if classified.lower().endswith(".copc.laz"):
                if run_pipeline([cls_path, {"type": "writers.copc", "filename": classified}], feedback) is None:
                    return {}
            elif classified.lower().endswith(".laz"):
                shutil.copyfile(cls_path, classified)
            else:
                if run_pipeline([cls_path, {"type": "writers.las", "filename": classified, "forward": "all",
                                            "minor_version": 4}], feedback) is None:
                    return {}
            results[self.CLASSIFIED] = classified
        feedback.setProgress(100)
        return results


class ElevationProfile(BiruniAlgorithm):
    ID = "lasprofile"
    NAME = "Elevation profile along lines"
    GROUP = "Point clouds & drones"
    GROUP_ID = "pointcloud"
    HELP = ("Samples a terrain or surface model (DTM, DSM or any DEM) along lines - roads, canals, cross-sections - "
            "every given distance. Outputs points with the distance from the start of the line and the elevation, "
            "and optionally a CSV file ready for a chart in Excel. Use 'selected features only' for one section.")

    INPUT, LINES, STEP, OUTPUT, CSV = "INPUT", "LINES", "STEP", "OUTPUT", "CSV"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, tr("Elevation (DTM / DSM)")))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.LINES, tr("Lines"), [Qgis.ProcessingSourceType.VectorLine]))
        self.addParameter(QgsProcessingParameterNumber(
            self.STEP, tr("Every (m, 0 = one cell)"), Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Profile points"), Qgis.ProcessingSourceType.VectorPoint))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.CSV, tr("Profile table (CSV for Excel)"), "CSV (*.csv)", optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        dem = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if dem is None:
            raise QgsProcessingException(self.invalidRasterError(parameters, self.INPUT))
        lines = self.parameterAsSource(parameters, self.LINES, context)
        if lines is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.LINES))
        rcrs = dem.crs()
        if rcrs.isGeographic():
            ext = dem.extent()
            c = ext.center()
            zone = int((c.x() + 180) // 6) + 1
            work_crs = QgsCoordinateReferenceSystem(f"EPSG:{(32600 if c.y() >= 0 else 32700) + zone}")
        else:
            work_crs = rcrs
        to_work = QgsCoordinateTransform(lines.sourceCrs(), work_crs, context.transformContext())
        to_dem = QgsCoordinateTransform(work_crs, rcrs, context.transformContext())
        step = self.parameterAsDouble(parameters, self.STEP, context)
        if not step:
            step = dem.rasterUnitsPerPixelX()
            if rcrs.isGeographic():
                da = QgsDistanceArea()
                da.setSourceCrs(rcrs, context.transformContext())
                da.setEllipsoid("EPSG:7030")
                c = dem.extent().center()
                step = da.measureLine(QgsPointXY(c.x(), c.y()), QgsPointXY(c.x() + step, c.y()))
        fields = QgsFields()
        for name, typ in (("line_id", QMetaType.Type.LongLong), ("distance", QMetaType.Type.Double),
                          ("elevation", QMetaType.Type.Double)):
            fields.append(QgsField(name, typ))
        sink, sid = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.Point, rcrs)
        provider = dem.dataProvider()
        rows = []
        stats = {"min": None, "max": None, "length": 0.0}
        total = lines.featureCount() or 1
        for n, f in enumerate(lines.getFeatures()):
            if feedback.isCanceled():
                return {}
            geom = QgsGeometry(f.geometry())
            if geom.isEmpty():
                continue
            geom.transform(to_work)
            parts = geom.asGeometryCollection() if geom.isMultipart() else [geom]
            offset = 0.0
            for part in parts:
                length = part.length()
                d = 0.0
                while True:
                    p = part.interpolate(min(d, length)).asPoint()
                    pd = QgsPointXY(to_dem.transform(p))
                    value, ok = provider.sample(pd, 1)
                    elev = float(value) if ok and value == value else None
                    feat = QgsFeature(fields)
                    feat.setGeometry(QgsGeometry.fromPointXY(pd))
                    feat.setAttributes([f.id(), round(offset + min(d, length), 3), elev])
                    sink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
                    rows.append((f.id(), offset + min(d, length), elev))
                    if elev is not None:
                        stats["min"] = elev if stats["min"] is None else min(stats["min"], elev)
                        stats["max"] = elev if stats["max"] is None else max(stats["max"], elev)
                    if d >= length:
                        break
                    d += step
                offset += length
            stats["length"] += offset
            feedback.setProgress(100 * (n + 1) / total)
        csv = self.parameterAsFileOutput(parameters, self.CSV, context)
        results = {self.OUTPUT: sid, "POINT_COUNT": len(rows), "MIN_ELEVATION": stats["min"],
                   "MAX_ELEVATION": stats["max"], "LENGTH": stats["length"]}
        if csv:
            import csv as csvmod

            with open(csv, "w", newline="", encoding="utf-8-sig") as fh:
                w = csvmod.writer(fh)
                w.writerow(["line_id", "distance_m", "elevation_m"])
                for lid, dist, elev in rows:
                    w.writerow([lid, f"{dist:.3f}", "" if elev is None else f"{elev:.3f}"])
            results[self.CSV] = csv
        if stats["min"] is not None:
            feedback.pushInfo(tr("{n} points along {l:.1f} m, elevation {a:.2f} to {b:.2f} m.").format(
                n=len(rows), l=stats["length"], a=stats["min"], b=stats["max"]))
        return results


class StockpileVolume(BiruniAlgorithm):
    ID = "lasvolume"
    NAME = "Stockpile and cut / fill volume"
    GROUP = "Point clouds & drones"
    GROUP_ID = "pointcloud"
    HELP = ("Measures the volume between a surface (a DSM or DTM from a drone or LiDAR survey, or any DEM) and a "
            "base inside each polygon - a stockpile, a pit, an earthwork area. The base can be the lowest point "
            "on the polygon's edge, the mean of its edge, a fixed level or another surface (for example the "
            "ground before the works). Gives the volume above the base (material, fill), below it (holes, cut) "
            "and the net, in cubic metres, with the area. The surface must be in a projected system (metres).")

    INPUT, POLYGONS, BASE, LEVEL, BASE_SURFACE, OUTPUT = "INPUT", "POLYGONS", "BASE", "LEVEL", "BASE_SURFACE", "OUTPUT"
    SIGMA_Z, REPORT = "SIGMA_Z", "REPORT"
    BASES = ["Lowest point of the edge", "Mean of the edge", "A fixed level", "Another surface"]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterRasterLayer(self.INPUT, tr("Surface (DSM / DTM)")))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.POLYGONS, tr("Areas (polygons)"), [Qgis.ProcessingSourceType.VectorPolygon]))
        self.addParameter(QgsProcessingParameterEnum(
            self.BASE, tr("Base"), [tr(b) for b in self.BASES], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.LEVEL, tr("Fixed level (m)"), Qgis.ProcessingNumberParameterType.Double, 0.0))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.BASE_SURFACE, tr("Base surface (before the works)"), optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.SIGMA_Z, tr("Vertical accuracy of the surface (m, one standard deviation)"),
            Qgis.ProcessingNumberParameterType.Double, 0.05, minValue=0.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Volumes"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.REPORT, tr("Volume report"), tr("Excel workbook") + " (*.xlsx);;PDF (*.pdf)", optional=True,
            createByDefault=False))

    @staticmethod
    def uncertainty(area, perimeter, cell_side, sigma_z, mean_thickness):
        """One-sigma volume uncertainty (m³): the surface's vertical error over the
        whole area (fully correlated, so conservative) combined with the edge
        error of cells half in, half out of the polygon."""
        vertical = sigma_z * area
        edge = perimeter * cell_side * abs(mean_thickness) / 4.0
        return math.hypot(vertical, edge)

    @staticmethod
    def _window(ds, rect):
        """Pixel window (xoff, yoff, w, h) of a rectangle in a north-up raster, clipped to the raster."""
        gt = ds.GetGeoTransform()
        x0 = int(math.floor((rect.xMinimum() - gt[0]) / gt[1]))
        x1 = int(math.ceil((rect.xMaximum() - gt[0]) / gt[1]))
        y0 = int(math.floor((rect.yMaximum() - gt[3]) / gt[5]))
        y1 = int(math.ceil((rect.yMinimum() - gt[3]) / gt[5]))
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, ds.RasterXSize), min(y1, ds.RasterYSize)
        return x0, y0, x1 - x0, y1 - y0

    @staticmethod
    def _mask(geom, gt, xoff, yoff, w, h, wkt):
        """Cells (by centre) inside the polygon."""
        from osgeo import gdal, ogr, osr

        drv = gdal.GetDriverByName("MEM")
        ds = drv.Create("", w, h, 1, gdal.GDT_Byte)
        ds.SetGeoTransform((gt[0] + xoff * gt[1], gt[1], 0, gt[3] + yoff * gt[5], 0, gt[5]))
        srs = osr.SpatialReference()
        if wkt:
            srs.ImportFromWkt(wkt)
        mem = ogr.GetDriverByName("Memory").CreateDataSource("")
        lyr = mem.CreateLayer("p", srs if wkt else None, ogr.wkbMultiPolygon)
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetGeometry(ogr.CreateGeometryFromWkb(bytes(geom.asWkb())))
        lyr.CreateFeature(feat)
        gdal.RasterizeLayer(ds, [1], lyr, burn_values=[1])
        return ds.GetRasterBand(1).ReadAsArray().astype(bool)

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        import numpy as np
        from osgeo import gdal

        gdal.UseExceptions()
        surf = self.parameterAsRasterLayer(parameters, self.INPUT, context)
        if surf is None:
            raise QgsProcessingException(self.invalidRasterError(parameters, self.INPUT))
        if surf.crs().isGeographic():
            raise QgsProcessingException(tr("The surface is in degrees: reproject it to a projected system "
                                            "(metres) first."))
        polys = self.parameterAsSource(parameters, self.POLYGONS, context)
        if polys is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.POLYGONS))
        base = self.parameterAsEnum(parameters, self.BASE, context)
        level = self.parameterAsDouble(parameters, self.LEVEL, context)
        base_layer = self.parameterAsRasterLayer(parameters, self.BASE_SURFACE, context)
        if base == 3 and base_layer is None:
            raise QgsProcessingException(tr("Choose the base surface."))

        ds = gdal.Open(surf.source())
        if ds is None:
            raise QgsProcessingException(tr("The surface could not be read."))
        gt = ds.GetGeoTransform()
        if gt[2] or gt[4]:
            raise QgsProcessingException(tr("Rotated rasters are not supported: warp the surface first."))
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        cell = abs(gt[1] * gt[5])
        wkt = ds.GetProjection()
        to_r = QgsCoordinateTransform(polys.sourceCrs(), surf.crs(), context.transformContext())

        fields = QgsFields(polys.fields())
        sigma_z = self.parameterAsDouble(parameters, self.SIGMA_Z, context)
        cell_side = math.sqrt(cell)
        names = [("area_m2", QMetaType.Type.Double), ("base_level", QMetaType.Type.Double),
                 ("above_m3", QMetaType.Type.Double), ("below_m3", QMetaType.Type.Double),
                 ("net_m3", QMetaType.Type.Double), ("surface_min", QMetaType.Type.Double),
                 ("surface_max", QMetaType.Type.Double), ("covered_pct", QMetaType.Type.Double),
                 ("uncert_m3", QMetaType.Type.Double), ("uncert95_m3", QMetaType.Type.Double)]
        report_rows = []
        label_field = next((fld.name() for fld in polys.fields() if fld.type() == QMetaType.Type.QString), None)
        for n, t in names:
            if fields.indexOf(n) >= 0:
                n = "v_" + n
            fields.append(QgsField(n, t))
        sink, sid = self.parameterAsSink(parameters, self.OUTPUT, context, fields,
                                         QgsWkbTypes.multiType(polys.wkbType()), polys.sourceCrs())
        totals = {"above": 0.0, "below": 0.0}
        total = polys.featureCount() or 1
        for i, f in enumerate(polys.getFeatures()):
            if feedback.isCanceled():
                return {}
            geom = QgsGeometry(f.geometry())
            if geom.isEmpty():
                continue
            geom.transform(to_r)
            xoff, yoff, w, h = self._window(ds, geom.boundingBox())
            vals = [None] * len(names)
            if w > 0 and h > 0:
                z = band.ReadAsArray(xoff, yoff, w, h).astype("float64")
                inside = self._mask(geom, gt, xoff, yoff, w, h, wkt)
                valid = inside & (z != nodata) & np.isfinite(z) if nodata is not None else inside & np.isfinite(z)
                n_in = int(inside.sum())
                if base == 3:
                    bz = self._resample(base_layer.source(), gt, xoff, yoff, w, h, wkt)
                    valid &= np.isfinite(bz)
                    ref = bz
                    base_value = float(np.nanmean(np.where(valid, bz, np.nan))) if valid.any() else None
                elif base == 2:
                    ref = base_value = level
                else:
                    edge = self._edge_values(geom, surf, max(abs(gt[1]), abs(gt[5])))
                    base_value = (min(edge) if base == 0 else sum(edge) / len(edge)) if edge else None
                    ref = base_value
                if base_value is not None and valid.any():
                    diff = np.where(valid, z - ref, 0.0)
                    above = float(diff[diff > 0].sum() * cell)
                    below = float(-diff[diff < 0].sum() * cell)
                    totals["above"] += above
                    totals["below"] += below
                    zv = z[valid]
                    area = n_in * cell
                    thickness = float(np.abs(diff[valid]).mean())
                    sigma = self.uncertainty(area, geom.constGet().perimeter(), cell_side, sigma_z, thickness)
                    vals = [round(area, 3), round(base_value, 3), round(above, 3), round(below, 3),
                            round(above - below, 3), round(float(zv.min()), 3), round(float(zv.max()), 3),
                            round(100.0 * valid.sum() / max(n_in, 1), 1), round(sigma, 3), round(1.96 * sigma, 3)]
                    report_rows.append({
                        "name": str(f[label_field]) if label_field and f[label_field] not in (None, "") else
                        str(f.id()), "area": area, "base": base_value, "above": above, "below": below,
                        "net": above - below, "sigma": sigma, "sigma95": 1.96 * sigma,
                        "covered": 100.0 * valid.sum() / max(n_in, 1)})
            out = QgsFeature(fields)
            out.setGeometry(f.geometry())
            out.setAttributes(list(f.attributes()) + vals)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            feedback.setProgress(100 * (i + 1) / total)
        feedback.pushInfo(tr("Above the base: {a:,.2f} m³ · below: {b:,.2f} m³ · net: {n:,.2f} m³.").format(
            a=totals["above"], b=totals["below"], n=totals["above"] - totals["below"]))
        results = {self.OUTPUT: sid, "ABOVE_M3": totals["above"], "BELOW_M3": totals["below"],
                   "NET_M3": totals["above"] - totals["below"]}
        report = self.parameterAsFileOutput(parameters, self.REPORT, context)
        if report:
            meta = {"surface": surf.name(), "base": tr(self.BASES[base]) + (f" ({level:g} m)" if base == 2 else ""),
                    "cell": cell_side, "sigma_z": sigma_z, "crs": surf.crs().authid()}
            if report.lower().endswith(".pdf"):
                write_volume_pdf(report, report_rows, meta)
            else:
                write_volume_xlsx(report, report_rows, meta)
            results[self.REPORT] = report
        return results

    @staticmethod
    def _edge_values(geom, layer, step):
        """Surface values along the outer edge of the polygon, every cell."""
        provider = layer.dataProvider()
        values = []
        for part in (geom.asGeometryCollection() if geom.isMultipart() else [geom]):
            ring = QgsGeometry.fromPolylineXY(part.asPolygon()[0]) if part.asPolygon() else None
            if ring is None:
                continue
            length = ring.length()
            d = 0.0
            while d < length:
                p = ring.interpolate(d).asPoint()
                v, ok = provider.sample(QgsPointXY(p), 1)
                if ok and v == v:
                    values.append(float(v))
                d += step
        return values

    @staticmethod
    def _resample(path, gt, xoff, yoff, w, h, wkt):
        """Another raster on the window grid of the surface (bilinear), NaN where empty."""
        import numpy as np
        from osgeo import gdal

        x0 = gt[0] + xoff * gt[1]
        y0 = gt[3] + yoff * gt[5]
        x1 = x0 + w * gt[1]
        y1 = y0 + h * gt[5]
        out = gdal.Warp("", path, format="MEM", outputBounds=(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
                        width=w, height=h, dstSRS=wkt or None, resampleAlg="bilinear", dstNodata=NODATA)
        a = out.GetRasterBand(1).ReadAsArray().astype("float64")
        a[a == NODATA] = np.nan
        return a


# ---- volume report -------------------------------------------------------------------

def _report_headers():
    return [tr("Area name"), tr("Area (m²)"), tr("Base level (m)"), tr("Above the base / fill (m³)"),
            tr("Below the base / cut (m³)"), tr("Net (m³)"), tr("Uncertainty ±1σ (m³)"), tr("Uncertainty 95% (m³)"),
            tr("Covered (%)")]


def _report_values(r):
    return [r["name"], round(r["area"], 2), round(r["base"], 3), round(r["above"], 2), round(r["below"], 2),
            round(r["net"], 2), round(r["sigma"], 2), round(r["sigma95"], 2), round(r["covered"], 1)]


def _report_notes(meta):
    return [(tr("Surface"), meta["surface"]), (tr("Coordinate system"), meta["crs"]),
            (tr("Base"), meta["base"]), (tr("Cell size (m)"), f"{meta['cell']:.3f}"),
            (tr("Vertical accuracy σz (m)"), f"{meta['sigma_z']:.3f}"),
            (tr("Uncertainty"), tr("σz × area (fully correlated) combined with the edge cells; 95% = 1.96σ."))]


def write_volume_xlsx(path, rows, meta):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    from ..i18n import is_rtl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = tr("Volumes")[:31]
    ws.sheet_view.rightToLeft = is_rtl()
    ws.append([tr("Stockpile and cut / fill volume")])
    ws["A1"].font = Font(bold=True, size=14)
    for k, v in _report_notes(meta):
        ws.append([k, v])
    ws.append([])
    head_row = ws.max_row + 1
    ws.append(_report_headers())
    for c in ws[head_row]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2F6F5E")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    first = ws.max_row + 1
    for r in rows:
        ws.append(_report_values(r))
    last = ws.max_row
    if rows:
        ws.append([tr("Total"), f"=SUM(B{first}:B{last})", "", f"=SUM(D{first}:D{last})",
                   f"=SUM(E{first}:E{last})", f"=SUM(F{first}:F{last})",
                   f"=SQRT(SUMSQ(G{first}:G{last}))", f"=SQRT(SUMSQ(H{first}:H{last}))", ""])
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
    for col, width in zip("ABCDEFGHI", (24, 12, 13, 18, 18, 14, 16, 16, 11)):
        ws.column_dimensions[col].width = width
    wb.save(path)


def write_volume_pdf(path, rows, meta):
    import html as h

    from qgis.PyQt.QtCore import QMarginsF, QSizeF
    from qgis.PyQt.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument

    from ..i18n import is_rtl

    direction = "rtl" if is_rtl() else "ltr"
    notes = "".join(f"<tr><td><b>{h.escape(k)}</b></td><td>{h.escape(str(v))}</td></tr>"
                    for k, v in _report_notes(meta))
    head = "".join(f"<th>{h.escape(x)}</th>" for x in _report_headers())
    body = "".join("<tr>" + "".join(f"<td>{h.escape(str(v))}</td>" for v in _report_values(r)) + "</tr>"
                   for r in rows)
    if rows:
        total = [tr("Total"), round(sum(r["area"] for r in rows), 2), "", round(sum(r["above"] for r in rows), 2),
                 round(sum(r["below"] for r in rows), 2), round(sum(r["net"] for r in rows), 2),
                 round(math.sqrt(sum(r["sigma"] ** 2 for r in rows)), 2),
                 round(math.sqrt(sum(r["sigma95"] ** 2 for r in rows)), 2), ""]
        body += "<tr>" + "".join(f"<td><b>{h.escape(str(v))}</b></td>" for v in total) + "</tr>"
    doc = QTextDocument()
    doc.setHtml(f"""<html><body dir="{direction}" style="font-family:'Segoe UI',Tahoma,Arial">
<h2>{h.escape(tr("Stockpile and cut / fill volume"))}</h2>
<table cellpadding="3">{notes}</table><br/>
<table border="1" cellspacing="0" cellpadding="4" width="100%"><tr style="background:#2F6F5E;color:white">{head}</tr>
{body}</table></body></html>""")
    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageOrientation(QPageLayout.Orientation.Landscape)
    writer.setPageMargins(QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter)
    writer.setResolution(150)
    doc.setPageSize(QSizeF(writer.width(), writer.height()))
    (getattr(doc, "print_", None) or getattr(doc, "print"))(writer)
