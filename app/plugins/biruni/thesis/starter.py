"""Starter pack for a study area (graduation project / thesis, chapter 1).

A place name, a drawn extent or a polygon layer becomes, within minutes, a
Biruni project folder with the basic free data of the area - each layer cut to
the study area, in a projected CRS (metres), styled and grouped:

* administrative boundaries (geoBoundaries) - country, first and second level
* OpenStreetMap roads, buildings, land use, water, places
* Copernicus DEM 30 m + slope, aspect, hillshade, contours
* ESA WorldCover 10 m land cover + an area table per class (km², %)
* Sentinel-2 true colour, false colour and NDVI of a chosen period
* WorldPop 2020 population (100 m, constrained) when it can be reached

plus a summary of the area, a "data sources" file with ready citations for the
references chapter, and a print layout for the location map of chapter 1
(country → region → study area).

The downloads reuse ``algorithms/open_data.py``. Everything that touches the
network goes through a *sources* object (``OnlineSources``) that tests replace
with a fake. Each source is independent: a failure is recorded with its reason
and the project is still built with what arrived.

``fetch()`` only writes files (safe in a Processing worker thread);
``build_project()`` makes the .qgz with the layer tree and the layout (main
thread). ``run()`` does both.
"""
import datetime as dt
import json
import math
import os
import re
import shutil
import tempfile
import time
import urllib.parse

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea, QgsFeature,
                       QgsField, QgsFields, QgsGeometry, QgsProcessingContext, QgsProcessingException,
                       QgsProcessingFeedback, QgsProject, QgsRectangle, QgsVectorFileWriter, QgsVectorLayer,
                       QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from ..version import APP_NAME, VERSION

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
FIGURE_KEY = "biruni/thesis-figure"
FIGURE = {"chapter": 1, "order": 1, "caption": "موقع منطقة الدراسة", "caption_en": "Location of the study area"}
NOMINATIM = "https://nominatim.openstreetmap.org/search"
WORLDPOP_API = "https://hub.worldpop.org/rest/data/pop/cic2020_100m?iso3={iso}"
WORLDPOP_MAX_BYTES = 400 * 1024 * 1024
OSM_KEYS = ("roads", "buildings", "landuse", "water", "places")
OSM_TITLES = {"roads": "Roads and streets", "buildings": "Buildings", "landuse": "Land use and parks",
              "water": "Water (rivers, lakes, canals)", "waterways": "Waterways (lines)",
              "places": "Places and services (POIs)"}
# modern national frames that are UTM-based: the study area gets the national version of its UTM zone
NATIONAL_FRAMES = {"SA": "sa-ksa-grf17"}

# group keys -> English titles (translated when the project is built)
GROUPS = [("area", "Study area"), ("admin", "Administrative boundaries"), ("osm", "OpenStreetMap"),
          ("terrain", "Terrain"), ("landcover", "Land cover"), ("imagery", "Satellite imagery (Sentinel-2)"),
          ("population", "Population")]


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------------------------------
def default_options():
    end = dt.date.today()
    return {"boundaries": True, "osm": list(OSM_KEYS), "terrain": True, "landcover": True, "sentinel": True,
            "s2_start": end - dt.timedelta(days=90), "s2_end": end, "cloud": 20, "population": True,
            "location_map": True, "contour_interval": 0}


# ---------------------------------------------------------------------------------------------------
# network sources (replace with a fake in tests)
# ---------------------------------------------------------------------------------------------------
def _ext_string(r):
    return f"{r.xMinimum():.7f},{r.xMaximum():.7f},{r.yMinimum():.7f},{r.yMaximum():.7f} [EPSG:4326]"


def _run_alg(cls, params, context, feedback):
    """A Biruni open-data algorithm as a child algorithm (registered id when available)."""
    import processing
    from qgis.core import QgsApplication

    alg_id = "biruni:" + cls.ID
    alg = alg_id if QgsApplication.processingRegistry().algorithmById(alg_id) else cls()
    return processing.run(alg, params, context=context, feedback=feedback, is_child_algorithm=True)


class OnlineSources:
    """The real downloads, through ``algorithms/open_data.py``."""

    def __init__(self, context=None):
        self.context = context

    def _ctx(self):
        if self.context is None:
            self.context = QgsProcessingContext()
            self.context.setProject(QgsProject.instance())
        return self.context

    # -- places ---------------------------------------------------------------
    def geocode(self, text, feedback=None, lang="en"):
        """Nominatim candidates: [{name, wkt (WGS 84 polygon or point), bbox (w, s, e, n), kind}]."""
        from ..algorithms.open_data import _geojson_to_wkt, http

        q = urllib.parse.urlencode({"q": text, "format": "jsonv2", "polygon_geojson": 1, "polygon_threshold": 0.0003,
                                    "limit": 6, "accept-language": lang or "en"})
        data = json.loads(http(f"{NOMINATIM}?{q}", feedback).decode("utf-8"))
        out = []
        for d in data:
            s, n, w, e = (float(v) for v in d.get("boundingbox", [0, 0, 0, 0]))
            gj = d.get("geojson") or {}
            wkt = _geojson_to_wkt(gj) if gj.get("type") in ("Polygon", "MultiPolygon") else ""
            if not wkt:
                wkt = f"POINT({d.get('lon')} {d.get('lat')})"
            out.append({"name": d.get("display_name", text), "wkt": wkt, "bbox": (w, s, e, n),
                        "kind": f"{d.get('category') or d.get('class', '')}/{d.get('type', '')}"})
        return out

    # -- geoBoundaries ---------------------------------------------------------
    def boundaries(self, iso3, level, folder, feedback=None):
        """GeoJSON path of a geoBoundaries level (simplified geometry) and the API record."""
        from ..algorithms.open_data import GEOBOUNDARIES, http

        info = json.loads(http(GEOBOUNDARIES.format(iso=iso3, level=level), feedback).decode("utf-8"))
        url = info.get("simplifiedGeometryGeoJSON") or info.get("gjDownloadURL")
        if not url:
            raise QgsProcessingException(tr("No boundaries were found for {c}.").format(c=iso3))
        path = os.path.join(folder, f"gb_{iso3}_adm{level}.geojson")
        with open(path, "wb") as f:
            f.write(http(url, feedback))
        return path, info

    # -- OpenStreetMap ------------------------------------------------------------
    def osm(self, key, rect, out, feedback=None):
        from ..algorithms import open_data

        index = [s[0] for s in open_data.OSM_SETS].index(key)
        res = _run_alg(open_data.DownloadOsm, {"EXTENT": _ext_string(rect), "DATA": index, "OUTPUT": out},
                       self._ctx(), feedback)
        return res["OUTPUT"]

    # -- elevation / land cover ----------------------------------------------------
    def dem(self, rect, out, feedback=None):
        from ..algorithms import open_data

        res = _run_alg(open_data.DownloadDem, {"EXTENT": _ext_string(rect), "HILLSHADE": False, "OUTPUT": out,
                                               "HILLSHADE_OUTPUT": None}, self._ctx(), feedback)
        return res["OUTPUT"]

    def landcover(self, rect, out, feedback=None):
        from ..algorithms import open_data

        res = _run_alg(open_data.DownloadLandCover, {"EXTENT": _ext_string(rect), "OUTPUT": out}, self._ctx(),
                       feedback)
        return res["OUTPUT"]

    # -- Sentinel-2 -----------------------------------------------------------------------
    def sentinel2(self, rect, start, end, cloud, folder, feedback=None):
        """Red, green, blue, NIR bands (UTM, 10 m or coarser for big areas) of the least-cloudy scenes of the
        period. Returns ({band: path}, [{id, date, cloud}], pixel size)."""
        from ..algorithms import open_data

        days = max(1, (end - start).days)
        items = open_data.stac_search(rect, days, cloud, feedback, end=end)
        scenes = open_data.pick_scenes(items, rect)
        if not scenes:
            raise QgsProcessingException(tr("No clear image was found. Look back more days or allow more clouds."))
        km2 = open_data.area_km2(rect)
        res = 10.0
        if km2 * 1e6 / 100 > 100e6:  # keep each band below ~100 million pixels
            res = math.ceil(math.sqrt(km2 * 1e6 / 100e6) / 10) * 10
        crs = open_data.utm_for(rect)
        order = list(reversed(scenes))  # best scene drawn last
        bands = {}
        for k, band in enumerate(("red", "green", "blue", "nir")):
            _cancel_check(feedback)
            path = os.path.join(folder, f"s2_{band}.tif")
            open_data._warp([s["assets"][band]["href"] for s in order], path, rect, crs, res, feedback,
                            progress=(k * 25, 25))
            bands[band] = path
        info = [{"id": s["id"], "date": s["properties"].get("datetime", "")[:10],
                 "cloud": round(float(s["properties"].get("eo:cloud_cover", 0)), 1)} for s in scenes]
        return bands, info, res

    # -- WorldPop -----------------------------------------------------------------------------
    def population(self, iso3, rect, folder, feedback=None):
        """Path of a WorldPop 2020 constrained (100 m) raster covering ``rect`` and the dataset record."""
        from osgeo import gdal
        from ..algorithms.open_data import http

        rec = json.loads(http(WORLDPOP_API.format(iso=iso3), feedback).decode("utf-8")).get("data") or []
        if not rec or not rec[0].get("files"):
            raise QgsProcessingException(tr("WorldPop has no 2020 population grid for {c}.").format(c=iso3))
        rec = rec[0]
        url = rec["files"][0]
        gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
        out = os.path.join(folder, "population_window.tif")
        try:  # GDAL may raise (UseExceptions) or return None when the server does not serve byte ranges
            src = gdal.Open("/vsicurl/" + url)
        except Exception:  # noqa: BLE001
            src = None
        if src is not None:
            try:
                window_translate(src, rect, out)
                return out, rec
            except Exception:  # noqa: BLE001 - the server may not serve byte ranges: download the file
                pass
            finally:
                src = None
        size = _content_length(url, feedback)
        if size and size > WORLDPOP_MAX_BYTES:
            raise QgsProcessingException(tr("The national population file is too large to download ({n} MB).")
                                         .format(n=round(size / 1048576)))
        whole = os.path.join(folder, "population_country.tif")
        with open(whole, "wb") as f:
            f.write(http(url, feedback))
        src = gdal.Open(whole)
        if src is None:
            raise QgsProcessingException(tr("The answer could not be read."))
        window_translate(src, rect, out)
        src = None
        return out, rec


def _content_length(url, feedback):
    from qgis.core import QgsBlockingNetworkRequest
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest

    from ..algorithms.open_data import USER_AGENT

    req = QNetworkRequest(QUrl(url))
    req.setRawHeader(b"User-Agent", USER_AGENT)
    br = QgsBlockingNetworkRequest()
    if br.head(req, True, feedback) != QgsBlockingNetworkRequest.ErrorCode.NoError:
        return 0
    try:
        return int(bytes(br.reply().rawHeader(b"Content-Length")).decode() or 0)
    except ValueError:
        return 0


def window_translate(src, rect, out):
    """Copy the pixels of a (geographic) raster that cover ``rect`` - on the source grid, no resampling."""
    from osgeo import gdal

    gt = src.GetGeoTransform()
    x0 = max(0, int(math.floor((rect.xMinimum() - gt[0]) / gt[1])) - 1)
    x1 = min(src.RasterXSize, int(math.ceil((rect.xMaximum() - gt[0]) / gt[1])) + 1)
    y0 = max(0, int(math.floor((rect.yMaximum() - gt[3]) / gt[5])) - 1)
    y1 = min(src.RasterYSize, int(math.ceil((rect.yMinimum() - gt[3]) / gt[5])) + 1)
    if x1 <= x0 or y1 <= y0:
        raise QgsProcessingException(tr("The study area is outside this dataset."))
    ds = gdal.Translate(out, src, srcWin=[x0, y0, x1 - x0, y1 - y0], creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
    if ds is None:
        raise QgsProcessingException(gdal.GetLastErrorMsg() or tr("The answer could not be read."))
    ds = None


# ---------------------------------------------------------------------------------------------------
# study area
# ---------------------------------------------------------------------------------------------------
class StudyArea:
    def __init__(self, geom_wgs84, name="", how=""):
        g = QgsGeometry(geom_wgs84)
        if g.type() == Qgis.GeometryType.Polygon:
            g = g.makeValid()
            from ..algorithms.base import polygonal

            g = polygonal(g) or g
        self.geometry = g          # WGS 84 (multi)polygon
        self.name = name or tr("Study area")
        self.how = how

    @property
    def rect(self):
        return self.geometry.boundingBox()


def area_from_extent(rect, crs, name=""):
    t = QgsCoordinateTransform(crs if crs and crs.isValid() else WGS84, WGS84, QgsProject.instance())
    g = QgsGeometry.fromRect(rect)
    g.transform(t)
    if g.isEmpty() or g.area() <= 0:
        raise QgsProcessingException(tr("Choose an area first (draw it or use the map extent)."))
    return StudyArea(g, name, "extent")


def area_from_layer(source, name=""):
    """All features of a polygon layer (or feature source), dissolved, in WGS 84."""
    t = QgsCoordinateTransform(source.sourceCrs() if hasattr(source, "sourceCrs") else source.crs(), WGS84,
                               QgsProject.instance())
    parts = []
    for f in source.getFeatures():
        g = QgsGeometry(f.geometry())
        if g.isEmpty() or g.type() != Qgis.GeometryType.Polygon:
            continue
        g.transform(t)
        parts.append(g)
    if not parts:
        raise QgsProcessingException(tr("The layer has no polygons."))
    return StudyArea(QgsGeometry.unaryUnion(parts), name or getattr(source, "name", lambda: "")(), "layer")


def area_from_candidate(cand, name=""):
    """A geocoder candidate -> study area; a point (or tiny polygon) becomes its bounding box, at least 2 km."""
    g = QgsGeometry.fromWkt(cand["wkt"])
    nm = name or cand["name"].split(",")[0].strip()
    if g.isEmpty() or g.type() != Qgis.GeometryType.Polygon:
        w, s, e, n = cand.get("bbox") or (0, 0, 0, 0)
        c = g.centroid().asPoint() if not g.isEmpty() else None
        r = QgsRectangle(w, s, e, n)
        if r.width() < 0.018 or r.height() < 0.018:
            x, y = (c.x(), c.y()) if c else (r.center().x(), r.center().y())
            dx = 0.009 / max(math.cos(math.radians(y)), 0.1)
            r = QgsRectangle(x - dx, y - 0.009, x + dx, y + 0.009)
        g = QgsGeometry.fromRect(r)
    return StudyArea(g, nm, "place")


def area_from_place(text, sources, feedback=None, lang="en"):
    cands = sources.geocode(text, feedback, lang)
    if not cands:
        raise QgsProcessingException(tr("No place called “{p}” was found.").format(p=text))
    best = next((c for c in cands if QgsGeometry.fromWkt(c["wkt"]).type() == Qgis.GeometryType.Polygon), cands[0])
    if feedback:
        feedback.pushInfo(tr("Place found: {n}").format(n=best["name"]))
    return area_from_candidate(best, text.split(",")[0].strip())


# ---------------------------------------------------------------------------------------------------
# CRS
# ---------------------------------------------------------------------------------------------------
def choose_crs(geom_wgs84):
    """(projected CRS in metres, reason): the national frame of the country when Biruni knows a modern one,
    otherwise the WGS 84 / UTM zone of the area's centre."""
    from .. import places, regional_crs
    from ..algorithms.open_data import utm_for

    r = geom_wgs84.boundingBox()
    c = geom_wgs84.centroid().asPoint() if not geom_wgs84.isEmpty() else r.center()
    utm = utm_for(QgsRectangle(c.x(), c.y(), c.x(), c.y()))
    zone = int((c.x() + 180) // 6) + 1
    hemi = "N" if c.y() >= 0 else "S"
    f = places.country_feature(c.x(), c.y())
    iso2 = (f["iso_a2"] if f is not None else "") or ""
    wanted = NATIONAL_FRAMES.get(iso2)
    if wanted:
        for e in regional_crs.for_country(iso2):
            if e["id"] != wanted:
                continue
            for code in e["codes"]:
                crs = QgsCoordinateReferenceSystem(code)
                if crs.isValid() and crs.description().endswith(f"UTM zone {zone}{hemi}"):
                    return crs, tr("national frame of the country, UTM zone {z}").format(z=f"{zone}{hemi}")
    reason = tr("UTM zone {z} of the area (metres)").format(z=f"{zone}{hemi}")
    if r.width() > 6:
        reason += " - " + tr("the area spans several UTM zones: lengths far from the centre are less exact")
    return utm, reason


# ---------------------------------------------------------------------------------------------------
# vector helpers
# ---------------------------------------------------------------------------------------------------
def _memory(wkb, crs, fields):
    if wkb == QgsWkbTypes.Type.NoGeometry:
        lyr = QgsVectorLayer("None", "tmp", "memory")
    else:
        lyr = QgsVectorLayer(QgsWkbTypes.displayString(wkb), "tmp", "memory")
        lyr.setCrs(crs)
    lyr.dataProvider().addAttributes(fields.toList())
    lyr.updateFields()
    return lyr


def write_table(gpkg, table, layer):
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = table
    opts.fileEncoding = "UTF-8"
    opts.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                                 if os.path.exists(gpkg) else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
    err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(layer, gpkg, QgsProject.instance().transformContext(),
                                                            opts)
    if err != QgsVectorFileWriter.WriterError.NoError:
        raise QgsProcessingException(msg)
    return f"{gpkg}|layername={table}"


def _of_type(g, gtype):
    if g is None or g.isEmpty():
        return None
    if gtype == Qgis.GeometryType.Polygon:
        from ..algorithms.base import polygonal

        return polygonal(g)
    if g.type() == gtype:
        return g
    parts = [p for p in g.asGeometryCollection() if p.type() == gtype]
    return QgsGeometry.collectGeometry(parts) if parts else None


def clip_vector(src, area, crs, feedback=None):
    """Features of ``src`` inside ``area`` (geometry in ``crs``), reprojected to ``crs``: a memory layer."""
    gtype = QgsWkbTypes.geometryType(src.wkbType())
    flat = QgsWkbTypes.flatType(src.wkbType())
    wkb = flat if gtype == Qgis.GeometryType.Point and not QgsWkbTypes.isMultiType(flat) else \
        QgsWkbTypes.multiType(flat)
    out = _memory(wkb, crs, src.fields())
    xf = QgsCoordinateTransform(src.crs(), crs, QgsProject.instance())
    engine = QgsGeometry.createGeometryEngine(area.constGet())
    engine.prepareGeometry()
    box = area.boundingBox()
    feats = []
    for f in src.getFeatures():
        if feedback is not None and feedback.isCanceled():
            break
        g = QgsGeometry(f.geometry())
        if g.isEmpty():
            continue
        try:
            g.transform(xf)
        except Exception:  # noqa: BLE001 - outside the projection's domain
            continue
        if not g.boundingBox().intersects(box) or not engine.intersects(g.constGet()):
            continue
        if not engine.contains(g.constGet()):
            g = _of_type(g.intersection(area), gtype)
            if g is None:
                continue
        if gtype != Qgis.GeometryType.Point:
            g.convertToMultiType()
        g = QgsGeometry(g)
        if QgsWkbTypes.hasZ(g.wkbType()) or QgsWkbTypes.hasM(g.wkbType()):
            g.get().dropZValue()
            g.get().dropMValue()
        nf = QgsFeature(out.fields())
        nf.setAttributes(f.attributes())
        nf.setGeometry(g)
        feats.append(nf)
    out.dataProvider().addFeatures(feats)
    out.updateExtents()
    return out


# ---------------------------------------------------------------------------------------------------
# raster helpers
# ---------------------------------------------------------------------------------------------------
def _same_srs(ds, crs):
    from osgeo import osr

    a = osr.SpatialReference(wkt=ds.GetProjection())
    b = osr.SpatialReference(wkt=crs.toWkt(Qgis.CrsWktVariant.Preferred if hasattr(Qgis, "CrsWktVariant") else 4))
    return bool(a.IsSame(b))


def clip_raster(src, dst, cut_gpkg, cut_layer, crs=None, nodata=None, resample="near"):
    """Mask a raster to the study polygon. Same CRS: the source grid is kept (no resampling, outside = no data);
    other CRS: warped and cropped to the polygon."""
    from osgeo import gdal

    ds = gdal.Open(src)
    if ds is None:
        raise QgsProcessingException(tr("The answer could not be read."))
    band = ds.GetRasterBand(1)
    if nodata is None:
        nodata = band.GetNoDataValue()
        if nodata is None:
            nodata = 0 if band.DataType == gdal.GDT_Byte else -9999
    kw = dict(format="GTiff", cutlineDSName=cut_gpkg, cutlineLayer=cut_layer, dstNodata=nodata, multithread=True,
              creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"])
    if crs is not None and not _same_srs(ds, crs):
        gt = ds.GetGeoTransform()
        kw.update(dstSRS=crs.toWkt(), cropToCutline=True, resampleAlg=resample, xRes=abs(gt[1]), yRes=abs(gt[5]))
    ds = None
    out = gdal.Warp(dst, src, options=gdal.WarpOptions(**kw))
    if out is None:
        raise QgsProcessingException(gdal.GetLastErrorMsg() or tr("The answer could not be read."))
    out = None
    return dst


def raster_values(path):
    """(masked numpy array of band 1, pixel area in m², geotransform)."""
    import numpy as np
    from osgeo import gdal

    ds = gdal.Open(path)
    b = ds.GetRasterBand(1)
    a = b.ReadAsArray().astype("float64")
    nd = b.GetNoDataValue()
    mask = ~np.isfinite(a)
    if nd is not None:
        mask |= a == nd
    gt = ds.GetGeoTransform()
    ds = None
    return np.ma.array(a, mask=mask), abs(gt[1] * gt[5]), gt


def class_areas(path, classes):
    """Land cover area per class: [(code, name, km², %)] sorted by area, % of the classified area. ``classes``
    = [(code, name, colour)]. Pixel size from the raster (projected, metres)."""
    import numpy as np

    a, px, _gt = raster_values(path)
    vals = a.compressed()
    vals = vals[vals > 0].astype("int64")
    if vals.size == 0:
        return []
    codes, counts = np.unique(vals, return_counts=True)
    total = float(counts.sum())
    names = {c: n for c, n, _col in classes}
    rows = [(int(c), tr(names.get(int(c), str(int(c)))), n * px / 1e6, n * 100.0 / total)
            for c, n in zip(codes.tolist(), counts.tolist())]
    return sorted(rows, key=lambda r: -r[2])


def contour_interval(zmin, zmax):
    from ..algorithms.terrain_style import nice_step

    return max(1.0, nice_step(max(zmax - zmin, 1.0), 12))


def make_contours(dem, out_gpkg, interval):
    from osgeo import gdal, ogr, osr

    ds = gdal.Open(dem)
    band = ds.GetRasterBand(1)
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(out_gpkg):
        drv.DeleteDataSource(out_gpkg)
    vds = drv.CreateDataSource(out_gpkg)
    srs = osr.SpatialReference(wkt=ds.GetProjection())
    lyr = vds.CreateLayer("contours", srs, ogr.wkbLineString)
    lyr.CreateField(ogr.FieldDefn("id", ogr.OFTInteger))
    lyr.CreateField(ogr.FieldDefn("elev", ogr.OFTReal))
    nd = band.GetNoDataValue()
    opts = [f"LEVEL_INTERVAL={interval}", "ID_FIELD=id", "ELEV_FIELD=elev"]
    if nd is not None:
        opts.append(f"NODATA={nd}")
    gdal.ContourGenerateEx(band, lyr, options=opts)
    vds = None
    ds = None
    return f"{out_gpkg}|layername=contours"


def stack_rgb(parts, out):
    from ..algorithms.open_data import _stack_rgb

    _stack_rgb(parts, out)


def ndvi(red, nir, out):
    from ..algorithms.open_data import _ndvi, _ndvi_style

    _ndvi(red, nir, out)
    _ndvi_style(out)


# ---------------------------------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------------------------------
def _cancel_check(feedback):
    if feedback is not None and feedback.isCanceled():
        raise Cancelled()


def _unique_folder(parent, name):
    from ..project import safe_name

    base = safe_name(name) or "Study area"
    cand, k = base, 2
    while os.path.exists(os.path.join(parent, cand)) and os.listdir(os.path.join(parent, cand)):
        cand, k = f"{base} ({k})", k + 1
    return cand


def fetch(area, parent, name, options=None, sources=None, feedback=None, context=None):
    """Create the project folder and download/derive every product. Returns the state for build_project()."""
    from ..project import create_folder_structure

    opts = default_options()
    opts.update(options or {})
    feedback = feedback or QgsProcessingFeedback()
    sources = sources or OnlineSources(context)
    folder_name = _unique_folder(parent, name)
    root, qgz, gpkg = create_folder_structure(parent, folder_name)
    crs, crs_reason = choose_crs(area.geometry)
    tmp = tempfile.mkdtemp(prefix="biruni_starter_")
    data = os.path.join(root, "data")
    area_proj = QgsGeometry(area.geometry)
    area_proj.transform(QgsCoordinateTransform(WGS84, crs, QgsProject.instance()))
    da = QgsDistanceArea()
    da.setEllipsoid("EPSG:7030")
    da.setSourceCrs(WGS84, QgsProject.instance().transformContext())
    area_km2 = da.measureArea(area.geometry) / 1e6
    state = {"name": area.name, "folder_name": folder_name, "root": root, "qgz": qgz, "gpkg": gpkg,
             "crs": crs.authid() or crs.toWkt(), "crs_name": crs.description(), "crs_reason": crs_reason,
             "how": area.how, "area_wkt_wgs84": area.geometry.asWkt(7), "layers": [], "results": [],
             "stats": {"area_km2": round(area_km2, 4)}, "sources": {}, "country": {}, "options": _jsonable(opts),
             "date": dt.date.today().isoformat(), "app": f"{APP_NAME} {VERSION}"}
    # the study area itself (also the cutline of every raster)
    fields = QgsFields()
    fields.append(QgsField("name", QMetaType.Type.QString))
    fields.append(QgsField("area_km2", QMetaType.Type.Double))
    lyr = _memory(QgsWkbTypes.Type.MultiPolygon, crs, fields)
    f = QgsFeature(lyr.fields())
    g = QgsGeometry(area_proj)
    g.convertToMultiType()
    f.setGeometry(g)
    f.setAttributes([area.name, round(area_km2, 4)])
    lyr.dataProvider().addFeatures([f])
    state["layers"].append({"group": "area", "name": tr("Study area"), "uri": write_table(gpkg, "study_area", lyr),
                            "kind": "vector", "style": "area"})
    cut = (gpkg, "study_area")
    ctx = {"area": area, "area_proj": area_proj, "crs": crs, "tmp": tmp, "data": data, "gpkg": gpkg, "cut": cut,
           "sources": sources, "feedback": feedback, "opts": opts, "state": state}

    steps = []
    if opts.get("boundaries") or opts.get("location_map"):
        steps.append(("boundaries", "Administrative boundaries (geoBoundaries)", _step_boundaries))
    for key in opts.get("osm") or []:
        steps.append((f"osm_{key}", f"OpenStreetMap: {OSM_TITLES.get(key, key)}",
                      lambda c, k=key: _step_osm(c, k)))
    if opts.get("terrain"):
        steps.append(("terrain", "Elevation (Copernicus DEM 30 m) and terrain", _step_terrain))
    if opts.get("landcover"):
        steps.append(("landcover", "Land cover (ESA WorldCover 10 m)", _step_landcover))
    if opts.get("sentinel"):
        steps.append(("sentinel2", "Satellite imagery (Sentinel-2)", _step_sentinel))
    if opts.get("population"):
        steps.append(("population", "Population (WorldPop 2020, 100 m)", _step_population))

    n = len(steps)
    cancelled = False
    for i, (key, title, fn) in enumerate(steps):
        rec = {"key": key, "title": title, "ok": None, "reason": "", "seconds": 0.0}
        state["results"].append(rec)
        if cancelled or feedback.isCanceled():
            cancelled = True
            rec["reason"] = tr("cancelled")
            continue
        feedback.setProgress(100.0 * i / max(n, 1))
        feedback.pushInfo("▶ " + title_text(title))
        t0 = time.time()
        try:
            note = fn(ctx)
            rec["ok"] = True
            rec["reason"] = note or ""
        except Cancelled:
            cancelled = True
            rec["ok"] = False
            rec["reason"] = tr("cancelled")
        except Exception as e:  # noqa: BLE001 - one source must never stop the others
            if feedback.isCanceled():
                cancelled = True
                rec["reason"] = tr("cancelled")
            else:
                rec["reason"] = str(e).strip() or type(e).__name__
            rec["ok"] = False
            feedback.pushWarning(f"{title_text(title)}: {rec['reason']}")
        rec["seconds"] = round(time.time() - t0, 1)
    feedback.setProgress(100)
    state["cancelled"] = cancelled
    write_summary(state)
    write_sources(state)
    shutil.rmtree(tmp, ignore_errors=True)
    with open(os.path.join(root, "outputs", "starter_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    return state


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    return o


def _add(ctx, group, name, uri, kind, style, **extra):
    d = {"group": group, "name": name, "uri": uri, "kind": kind, "style": style}
    d.update(extra)
    ctx["state"]["layers"].append(d)


# ---- boundaries -----------------------------------------------------------------------------------
def _gb_layer(path):
    lyr = QgsVectorLayer(path, "gb", "ogr")
    if not lyr.isValid():
        raise QgsProcessingException(tr("The answer could not be read."))
    return lyr


def _containing(lyr, pt_wgs84):
    xf = QgsCoordinateTransform(WGS84, lyr.crs(), QgsProject.instance())
    p = QgsGeometry.fromPointXY(pt_wgs84)
    p.transform(xf)
    for f in lyr.getFeatures():
        if f.geometry().contains(p):
            return f
    return None


def _feature_name(f):
    if f is None:
        return ""
    for k in ("shapeName", "name", "NAME", "shapename"):
        if k in f.fields().names() and f[k]:
            return str(f[k])
    return ""


def _step_boundaries(ctx):
    from .. import places

    st, area, crs, fb = ctx["state"], ctx["area"], ctx["crs"], ctx["feedback"]
    c = area.geometry.centroid().asPoint()
    cf = places.country_feature(c.x(), c.y())
    iso3 = places.country_iso3(c.x(), c.y()) or ""
    st["country"] = {"iso3": iso3, "iso2": cf["iso_a2"] if cf is not None else "",
                     "name": cf["name"] if cf is not None else "", "name_ar": cf["name_ar"] if cf is not None else ""}
    notes = []
    gb = {}
    if iso3:
        for level in (0, 1, 2):
            _cancel_check(fb)
            try:
                path, info = ctx["sources"].boundaries(iso3, level, ctx["tmp"], fb)
                gb[level] = (_gb_layer(path), info)
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                notes.append(tr("level {n}: {e}").format(n=level, e=str(e).strip()))
    st["sources"]["geoboundaries"] = {lv: {k: info.get(k, "") for k in ("boundaryName", "boundaryISO", "boundaryType",
                                                                         "boundaryYearRepresented", "boundarySource",
                                                                         "boundaryLicense", "boundaryCanonical")}
                                      for lv, (_l, info) in gb.items()}
    # country outline: geoBoundaries level 0, else the bundled Natural Earth country (offline)
    if 0 in gb:
        country = clip_vector(gb[0][0], _whole(gb[0][0], crs), crs)
        st["country"]["name"] = _feature_name(next(gb[0][0].getFeatures(), None)) or st["country"]["name"]
    elif cf is not None:
        country = _memory(QgsWkbTypes.Type.MultiPolygon, crs, cf.fields())
        nf = QgsFeature(country.fields())
        nf.setAttributes(cf.attributes())
        g = QgsGeometry(cf.geometry())
        g.transform(QgsCoordinateTransform(WGS84, crs, QgsProject.instance()))
        g.convertToMultiType()
        nf.setGeometry(g)
        country.dataProvider().addFeatures([nf])
        st["sources"]["naturalearth"] = True
        notes.append(tr("the country outline comes from the bundled Natural Earth data"))
    else:
        raise QgsProcessingException(tr("No country was found in the middle of the area."))
    _add(ctx, "admin", tr("Country"), write_table(ctx["gpkg"], "admin_country", country), "vector", "country",
         location=True)
    if 1 in gb:
        lyr1 = gb[1][0]
        _add(ctx, "admin", tr("First-level divisions"), write_table(ctx["gpkg"], "admin_level1",
                                                                    clip_vector(lyr1, _whole(lyr1, crs), crs)),
             "vector", "admin1", location=True)
        region = _containing(lyr1, c)
        if region is not None:
            st["country"]["region"] = _feature_name(region)
            reg = _memory(QgsWkbTypes.Type.MultiPolygon, crs, lyr1.fields())
            nf = QgsFeature(reg.fields())
            nf.setAttributes(region.attributes())
            g = QgsGeometry(region.geometry())
            g.transform(QgsCoordinateTransform(lyr1.crs(), crs, QgsProject.instance()))
            g.convertToMultiType()
            nf.setGeometry(g)
            reg.dataProvider().addFeatures([nf])
            _add(ctx, "admin", tr("Region of the study area"), write_table(ctx["gpkg"], "admin_region", reg),
                 "vector", "region", location=True)
    if 2 in gb:
        lyr2 = clip_vector(gb[2][0], ctx["area_proj"], crs)
        if lyr2.featureCount():
            _add(ctx, "admin", tr("Second-level divisions (in the study area)"),
                 write_table(ctx["gpkg"], "admin_level2", lyr2), "vector", "admin2")
            st["stats"]["admin2_units"] = [_feature_name(f) for f in lyr2.getFeatures()]
    if not gb and not iso3:
        raise QgsProcessingException(tr("No country was found in the middle of the area."))
    if iso3 and not gb:
        notes.insert(0, tr("geoBoundaries could not be reached"))
    return "; ".join(notes)


def _whole(lyr, crs):
    """A polygon (in ``crs``) that contains the whole layer: its reprojected extent, slightly enlarged."""
    xf = QgsCoordinateTransform(lyr.crs(), crs, QgsProject.instance())
    r = xf.transformBoundingBox(lyr.extent())
    r.grow(max(r.width(), r.height()) * 0.05 + 1)
    return QgsGeometry.fromRect(r)


# ---- OpenStreetMap ---------------------------------------------------------------------------------
def _step_osm(ctx, key):
    st, fb = ctx["state"], ctx["feedback"]
    keys = [key, "waterways"] if key == "water" else [key]
    notes = []
    got = 0
    done = 0
    for k in keys:
        _cancel_check(fb)
        try:
            path = ctx["sources"].osm(k, ctx["area"].rect, os.path.join(ctx["tmp"], f"osm_{k}.gpkg"), fb)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            if len(keys) == 1:
                raise
            _cancel_check(fb)
            notes.append(f"{tr(OSM_TITLES[k])}: {str(e).strip()}")
            continue
        done += 1
        src = QgsVectorLayer(path, k, "ogr")
        if not src.isValid():
            raise QgsProcessingException(tr("The answer could not be read."))
        lyr = clip_vector(src, ctx["area_proj"], ctx["crs"], fb)
        _cancel_check(fb)
        uri = write_table(ctx["gpkg"], f"osm_{k}", lyr)
        _add(ctx, "osm", tr(OSM_TITLES[k]), uri, "vector", k)
        got += lyr.featureCount()
        if k == "roads":
            st["stats"]["roads_km_by_type"] = road_lengths(lyr)
            st["stats"]["roads_km"] = round(sum(st["stats"]["roads_km_by_type"].values()), 3)
        elif k == "buildings":
            st["stats"]["buildings"] = lyr.featureCount()
            st["stats"]["buildings_km2"] = round(sum(f.geometry().area() for f in lyr.getFeatures()) / 1e6, 4)
        elif k == "places":
            st["stats"]["places"] = lyr.featureCount()
    if not done:
        raise QgsProcessingException("; ".join(notes))
    st["sources"]["osm"] = True
    return "; ".join([tr("{n} features").format(n=got)] + notes)


def road_lengths(lyr):
    """{highway type: km} of a projected road layer, longest first."""
    out = {}
    names = lyr.fields().names()
    for f in lyr.getFeatures():
        t = (f["highway"] if "highway" in names else None) or "other"
        out[t] = out.get(t, 0.0) + f.geometry().length() / 1000.0
    return {k: round(v, 3) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


# ---- terrain ---------------------------------------------------------------------------------------------
def _step_terrain(ctx):
    from osgeo import gdal

    st, fb, tmp, data = ctx["state"], ctx["feedback"], ctx["tmp"], ctx["data"]
    r = QgsRectangle(ctx["area"].rect)
    r.grow(0.002)  # a few pixels around: no edge effects in slope / hillshade
    raw = ctx["sources"].dem(r, os.path.join(tmp, "dem_raw.tif"), fb)
    _cancel_check(fb)
    derived = {}
    for kind in ("slope", "aspect", "hillshade"):
        p = os.path.join(tmp, f"{kind}_raw.tif")
        o = gdal.DEMProcessingOptions(computeEdges=True, creationOptions=["COMPRESS=DEFLATE"],
                                      **({"multiDirectional": True} if kind == "hillshade" else {}))
        if gdal.DEMProcessing(p, raw, kind, options=o) is None:
            raise QgsProcessingException(gdal.GetLastErrorMsg())
        derived[kind] = p
    _cancel_check(fb)
    gpkg, layer = ctx["cut"]
    dem = clip_raster(raw, os.path.join(data, "elevation_dem.tif"), gpkg, layer, ctx["crs"], resample="bilinear")
    a, _px, _gt = raster_values(dem)
    if a.count() == 0:
        raise QgsProcessingException(tr("There is no elevation data here (sea or outside the coverage)."))
    zmin, zmax, zmean = float(a.min()), float(a.max()), float(a.mean())
    st["stats"].update({"elev_min": round(zmin, 1), "elev_max": round(zmax, 1), "elev_mean": round(zmean, 1)})
    _add(ctx, "terrain", tr("Elevation (m)"), dem, "raster", "dem")
    out = {}
    for kind, title in (("hillshade", "Hillshade"), ("slope", "Slope (degrees)"), ("aspect", "Aspect (degrees)")):
        out[kind] = clip_raster(derived[kind], os.path.join(data, f"{kind}.tif"), gpkg, layer, ctx["crs"],
                                resample="bilinear")
    s, _px, _gt = raster_values(out["slope"])
    if s.count():
        st["stats"]["slope_mean"] = round(float(s.mean()), 2)
        st["stats"]["slope_max"] = round(float(s.max()), 2)
    interval = float(ctx["opts"].get("contour_interval") or 0) or contour_interval(zmin, zmax)
    st["stats"]["contour_interval"] = interval
    _add(ctx, "terrain", tr("Contours ({i} m)").format(i=f"{interval:g}"), "", "vector", "contours",
         interval=interval)
    cont_src = QgsVectorLayer(make_contours(raw, os.path.join(tmp, "contours.gpkg"), interval), "c", "ogr")
    st["layers"][-1]["uri"] = write_table(ctx["gpkg"], "contours", clip_vector(cont_src, ctx["area_proj"],
                                                                               ctx["crs"], fb))
    _add(ctx, "terrain", tr("Hillshade"), out["hillshade"], "raster", "hillshade")
    _add(ctx, "terrain", tr("Slope (degrees)"), out["slope"], "raster", "slope", visible=False)
    _add(ctx, "terrain", tr("Aspect (degrees)"), out["aspect"], "raster", "aspect", visible=False)
    st["sources"]["dem"] = True
    return ""


# ---- land cover -----------------------------------------------------------------------------------------
def _step_landcover(ctx):
    import csv

    from ..algorithms.open_data import WORLDCOVER_CLASSES, _worldcover_style

    st, fb = ctx["state"], ctx["feedback"]
    raw = ctx["sources"].landcover(ctx["area"].rect, os.path.join(ctx["tmp"], "landcover_raw.tif"), fb)
    _cancel_check(fb)
    gpkg, layer = ctx["cut"]
    lc = clip_raster(raw, os.path.join(ctx["data"], "landcover.tif"), gpkg, layer, ctx["crs"], nodata=0)
    _worldcover_style(lc)
    rows = class_areas(lc, WORLDCOVER_CLASSES)
    fields = QgsFields()
    for n, t in (("code", QMetaType.Type.Int), ("class", QMetaType.Type.QString), ("area_km2", QMetaType.Type.Double),
                 ("percent", QMetaType.Type.Double)):
        fields.append(QgsField(n, t))
    tab = _memory(QgsWkbTypes.Type.NoGeometry, None, fields)
    feats = []
    for code, name, km2, pct in rows:
        f = QgsFeature(tab.fields())
        f.setAttributes([code, name, round(km2, 4), round(pct, 2)])
        feats.append(f)
    tab.dataProvider().addFeatures(feats)
    _add(ctx, "landcover", tr("Land cover (ESA WorldCover)"), lc, "raster", "landcover")
    _add(ctx, "landcover", tr("Land cover area per class"), write_table(ctx["gpkg"], "landcover_area", tab), "table",
         "table")
    csv_path = os.path.join(st["root"], "outputs", "landcover_area.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["code", "class", "area_km2", "percent"])
        for code, name, km2, pct in rows:
            w.writerow([code, name, f"{km2:.4f}", f"{pct:.2f}"])
    st["stats"]["landcover"] = [{"code": c, "class": n, "km2": round(k, 4), "percent": round(p, 2)}
                                for c, n, k, p in rows]
    st["sources"]["worldcover"] = True
    return ""


# ---- Sentinel-2 ------------------------------------------------------------------------------------------
def _date(v):
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def _step_sentinel(ctx):
    st, fb, opts = ctx["state"], ctx["feedback"], ctx["opts"]
    start, end = _date(opts["s2_start"]), _date(opts["s2_end"])
    if end < start:
        start, end = end, start
    bands, scenes, res = ctx["sources"].sentinel2(ctx["area"].rect, start, end, int(opts.get("cloud", 20)),
                                                  ctx["tmp"], fb)
    _cancel_check(fb)
    gpkg, layer = ctx["cut"]
    clipped = {b: clip_raster(p, os.path.join(ctx["tmp"], f"cut_{b}.tif"), gpkg, layer, ctx["crs"], nodata=0,
                              resample="bilinear") for b, p in bands.items()}
    rgb = os.path.join(ctx["data"], "sentinel2_true_colour.tif")
    stack_rgb([clipped["red"], clipped["green"], clipped["blue"]], rgb)
    fcc = os.path.join(ctx["data"], "sentinel2_false_colour.tif")
    stack_rgb([clipped["nir"], clipped["red"], clipped["green"]], fcc)
    nd = os.path.join(ctx["data"], "sentinel2_ndvi.tif")
    ndvi(clipped["red"], clipped["nir"], nd)
    v, _px, _gt = raster_values(nd)
    if v.count():
        st["stats"]["ndvi_mean"] = round(float(v.mean()), 3)
    _add(ctx, "imagery", tr("Sentinel-2 true colour"), rgb, "raster", "rgb")
    _add(ctx, "imagery", tr("Sentinel-2 false colour (vegetation in red)"), fcc, "raster", "rgb", visible=False)
    _add(ctx, "imagery", tr("NDVI (vegetation index)"), nd, "raster", "ndvi", visible=False)
    st["sources"]["sentinel2"] = {"scenes": scenes, "start": start.isoformat(), "end": end.isoformat(),
                                  "resolution": res}
    return tr("{n} scene(s): {d}").format(n=len(scenes), d=", ".join(s["date"] for s in scenes))


# ---- population -------------------------------------------------------------------------------------------
def _step_population(ctx):
    from .. import places

    st, fb = ctx["state"], ctx["feedback"]
    c = ctx["area"].geometry.centroid().asPoint()
    iso3 = (st.get("country") or {}).get("iso3") or places.country_iso3(c.x(), c.y())
    if not iso3:
        raise QgsProcessingException(tr("No country was found in the middle of the area."))
    window, rec = ctx["sources"].population(iso3, ctx["area"].rect, ctx["tmp"], fb)
    _cancel_check(fb)
    gpkg, layer = ctx["cut"]
    # kept on WorldPop's own geographic grid: re-sampling would change the head counts
    pop = clip_raster(window, os.path.join(ctx["data"], "population_worldpop_2020.tif"), gpkg, layer, None)
    a, _px, _gt = raster_values(pop)
    total = float(a.clip(min=0).sum()) if a.count() else 0.0
    st["stats"]["population"] = round(total)
    if st["stats"].get("area_km2"):
        st["stats"]["population_density"] = round(total / st["stats"]["area_km2"], 1)
    _add(ctx, "population", tr("Population 2020 (people per 100 m cell)"), pop, "raster", "population")
    st["sources"]["worldpop"] = {k: rec.get(k, "") for k in ("title", "doi", "citation", "license", "date",
                                                            "data_file")}
    return tr("about {n} people").format(n=f"{round(total):,}")


# ---------------------------------------------------------------------------------------------------
# summary and citations
# ---------------------------------------------------------------------------------------------------
def write_summary(state):
    s = state["stats"]
    lines = [f"# {tr('Study area summary')}: {state['name']}", "",
             f"- {tr('Date')}: {state['date']}",
             f"- {tr('Coordinate system')}: {state['crs_name']} ({state['crs']}) - {state['crs_reason']}",
             f"- {tr('Area')}: {s.get('area_km2', 0):,.3f} km²"]
    c = state.get("country") or {}
    if c.get("name"):
        lines.append(f"- {tr('Country')}: {c['name']}" + (f" - {tr('Region')}: {c['region']}" if c.get("region") else ""))
    if "elev_min" in s:
        lines.append(f"- {tr('Elevation')}: {tr('min')} {s['elev_min']:,.1f} m, {tr('mean')} {s['elev_mean']:,.1f} m, "
                     f"{tr('max')} {s['elev_max']:,.1f} m")
    if "slope_mean" in s:
        lines.append(f"- {tr('Slope')}: {tr('mean')} {s['slope_mean']}°, {tr('max')} {s['slope_max']}°")
    if "buildings" in s:
        lines.append(f"- {tr('Buildings (OpenStreetMap)')}: {s['buildings']:,} ({s.get('buildings_km2', 0):,.3f} km²)")
    if "population" in s:
        lines.append(f"- {tr('Population 2020 (WorldPop)')}: {s['population']:,} "
                     f"({s.get('population_density', 0):,.1f} / km²)")
    if "ndvi_mean" in s:
        lines.append(f"- {tr('Mean NDVI')}: {s['ndvi_mean']}")
    if s.get("roads_km_by_type"):
        lines += ["", f"## {tr('Road length by type (OpenStreetMap)')}", "",
                  f"| {tr('Type')} | km |", "|---|---:|"]
        lines += [f"| {k} | {v:,.3f} |" for k, v in s["roads_km_by_type"].items()]
        lines.append(f"| **{tr('Total')}** | **{s['roads_km']:,.3f}** |")
    if s.get("landcover"):
        lines += ["", f"## {tr('Land cover (ESA WorldCover 2021)')}", "",
                  f"| {tr('Class')} | km² | % |", "|---|---:|---:|"]
        lines += [f"| {r['class']} | {r['km2']:,.4f} | {r['percent']:.2f} |" for r in s["landcover"]]
    lines += ["", f"## {tr('What was downloaded')}", ""]
    for r in state["results"]:
        mark = "✔" if r["ok"] else ("✖" if r["ok"] is False else "–")
        lines.append(f"- {mark} {title_text(r['title'])} ({r['seconds']} s)" + (f": {r['reason']}" if r["reason"] else ""))
    path = os.path.join(state["root"], "SUMMARY.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(state["root"], "outputs", "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"name": state["name"], "crs": state["crs"], "stats": s, "results": state["results"]}, f,
                  ensure_ascii=False, indent=1)
    state["summary"] = path
    return path


def citations(state):
    """[(key, dataset, provider, licence, data date, resolution, citation, bibtex)] of the sources that worked."""
    acc = state["date"]
    year = acc[:4]
    src = state.get("sources", {})
    out = []
    if src.get("geoboundaries"):
        any_rec = next(iter(src["geoboundaries"].values()), {})
        yrs = sorted({str(v.get("boundaryYearRepresented", "")) for v in src["geoboundaries"].values()} - {""})
        out.append(("geoboundaries", f"geoBoundaries gbOpen - {any_rec.get('boundaryName') or ''} ADM"
                    + "/".join(str(k) for k in sorted(src["geoboundaries"])),
                    "William & Mary geoLab; sources: " + (any_rec.get("boundarySource") or "national agencies"),
                    any_rec.get("boundaryLicense") or "CC BY 4.0", ", ".join(yrs) or "-", "vector (simplified)",
                    "Runfola, D., Anderson, A., Baier, H., Crittenden, M., Dowker, E., Fuhrig, S., et al. (2020). "
                    "geoBoundaries: A global database of political administrative boundaries. PLoS ONE, 15(4), "
                    "e0231866. https://doi.org/10.1371/journal.pone.0231866",
                    "@article{runfola2020geoboundaries,\n  author = {Runfola, Daniel and others},\n  title = "
                    "{geoBoundaries: A global database of political administrative boundaries},\n  journal = "
                    "{PLoS ONE},\n  volume = {15},\n  number = {4},\n  pages = {e0231866},\n  year = {2020},\n  doi = "
                    "{10.1371/journal.pone.0231866}\n}"))
    if src.get("naturalearth"):
        out.append(("naturalearth", "Natural Earth - Admin 0 countries (bundled with Biruni GIS)", "Natural Earth",
                    "Public domain", "-", "1:10 000 000 / 1:50 000 000",
                    "Natural Earth. (n.d.). Admin 0 - Countries [Data set]. https://www.naturalearthdata.com/",
                    "@misc{naturalearth,\n  author = {{Natural Earth}},\n  title = {Admin 0 - Countries},\n  "
                    "howpublished = {\\url{https://www.naturalearthdata.com/}}\n}"))
    if src.get("osm"):
        out.append(("osm", "OpenStreetMap (roads, buildings, land use, water, places)", "OpenStreetMap contributors "
                    "(via the Overpass API)", "ODbL 1.0", acc, "vector",
                    f"OpenStreetMap contributors. ({year}). OpenStreetMap [Data set]. Retrieved {acc} from "
                    "https://www.openstreetmap.org via the Overpass API. Available under the Open Database "
                    "Licence (ODbL).",
                    f"@misc{{osm{year},\n  author = {{{{OpenStreetMap contributors}}}},\n  title = {{OpenStreetMap}},"
                    f"\n  year = {{{year}}},\n  howpublished = {{\\url{{https://www.openstreetmap.org}}}},\n  note = "
                    f"{{Retrieved {acc} via the Overpass API, ODbL}}\n}}"))
    if src.get("dem"):
        out.append(("dem", "Copernicus DEM GLO-30", "European Space Agency / Copernicus (AWS open data)",
                    "Copernicus DEM licence (free, attribution)", "2011-2015 (TanDEM-X)", "30 m",
                    "European Space Agency. (2024). Copernicus Global Digital Elevation Model (GLO-30) [Data set]. "
                    "https://doi.org/10.5270/ESA-c5d3d65. Produced using Copernicus WorldDEM-30 © DLR e.V. "
                    "2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the "
                    "European Union and ESA; all rights reserved.",
                    "@misc{copernicusdem,\n  author = {{European Space Agency}},\n  title = {Copernicus Global "
                    "Digital Elevation Model (GLO-30)},\n  year = {2024},\n  doi = {10.5270/ESA-c5d3d65}\n}"))
    if src.get("worldcover"):
        out.append(("worldcover", "ESA WorldCover 10 m 2021 v200", "European Space Agency (ESA WorldCover "
                    "consortium)", "CC BY 4.0", "2021", "10 m",
                    "Zanaga, D., Van De Kerchove, R., Daems, D., De Keersmaecker, W., Brockmann, C., Kirches, G., "
                    "Wevers, J., Cartus, O., Santoro, M., Fritz, S., Lesiv, M., Herold, M., Tsendbazar, N.E., Xu, "
                    "P., Ramoino, F., & Arino, O. (2022). ESA WorldCover 10 m 2021 v200 [Data set]. Zenodo. "
                    "https://doi.org/10.5281/zenodo.7254221",
                    "@dataset{zanaga2022worldcover,\n  author = {Zanaga, Daniele and others},\n  title = {ESA "
                    "WorldCover 10 m 2021 v200},\n  year = {2022},\n  publisher = {Zenodo},\n  doi = "
                    "{10.5281/zenodo.7254221}\n}"))
    s2 = src.get("sentinel2")
    if s2:
        dates = ", ".join(sorted({s["date"] for s in s2["scenes"]}))
        ids = ", ".join(s["id"] for s in s2["scenes"])
        out.append(("sentinel2", "Sentinel-2 Level-2A (true colour, false colour, NDVI)",
                    "European Space Agency / Copernicus; Earth Search STAC by Element 84 (AWS open data)",
                    "Copernicus Sentinel data terms (free, attribution)", dates, f"{s2['resolution']:g} m",
                    f"Contains modified Copernicus Sentinel data [{dates[:4]}] processed by ESA. European Space "
                    f"Agency. Sentinel-2 MSI Level-2A surface reflectance, scenes {ids}. Retrieved {acc} from Earth "
                    "Search (Element 84), https://earth-search.aws.element84.com/v1",
                    f"@misc{{sentinel2,\n  author = {{{{European Space Agency}}}},\n  title = {{Copernicus Sentinel-2 "
                    f"MSI Level-2A, scenes {ids}}},\n  year = {{{dates[:4]}}},\n  howpublished = "
                    f"{{\\url{{https://earth-search.aws.element84.com/v1}}}},\n  note = {{Retrieved {acc}}}\n}}"))
    wp = src.get("worldpop")
    if wp:
        cite = wp.get("citation") or ("Bondarenko M., Kerr D., Sorichetta A., and Tatem, A.J. 2020. "
                                      "Census/projection-disaggregated gridded population datasets for 189 countries "
                                      "in 2020 using Built-Settlement Growth Model (BSGM) outputs. WorldPop, "
                                      "University of Southampton, UK. doi:10.5258/SOTON/WP00684")
        doi = wp.get("doi") or "10.5258/SOTON/WP00684"
        out.append(("worldpop", wp.get("title") or "WorldPop 2020 constrained population counts",
                    "WorldPop, University of Southampton", "CC BY 4.0", "2020", "3 arc-seconds (about 100 m)", cite,
                    "@misc{worldpop2020,\n  author = {Bondarenko, Maksym and Kerr, David and Sorichetta, Alessandro "
                    "and Tatem, Andrew J.},\n  title = {Census/projection-disaggregated gridded population datasets "
                    "for 189 countries in 2020 using Built-Settlement Growth Model (BSGM) outputs},\n  year = "
                    f"{{2020}},\n  publisher = {{WorldPop, University of Southampton}},\n  doi = {{{doi}}}\n}}"))
    return out


def write_sources(state):
    rows = citations(state)
    lines = [f"# {tr('Data sources')} - {state['name']}", "",
             tr("Accessed on {d} with {a}. Every layer was cut to the study area and projected to {c}.").format(
                 d=state["date"], a=state["app"], c=f"{state['crs_name']} ({state['crs']})"), ""]
    for _k, name, prov, lic, date, res, cite, _bib in rows:
        lines += [f"## {name}", "", f"- {tr('Provider')}: {prov}", f"- {tr('Licence')}: {lic}",
                  f"- {tr('Data date')}: {date}", f"- {tr('Resolution')}: {res}", "",
                  f"{tr('Citation')}:", "", f"> {cite}", ""]
    failed = [r for r in state["results"] if r["ok"] is False]
    if failed:
        lines += [f"## {tr('Not downloaded')}", ""]
        lines += [f"- {title_text(r['title'])}: {r['reason']}" for r in failed]
        lines.append("")
    path = os.path.join(state["root"], "DATA_SOURCES.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(os.path.join(state["root"], "references.bib"), "w", encoding="utf-8") as f:
        f.write("\n\n".join(r[7] for r in rows) + "\n")
    state["sources_file"] = path
    return path


# ---------------------------------------------------------------------------------------------------
# the project
# ---------------------------------------------------------------------------------------------------
def build_project(state):
    """Write the .qgz of a fetched starter pack: layer groups, styles, the location layout. Main thread."""
    from qgis.core import QgsRasterLayer

    from . import starter_style as sty

    prj = QgsProject()
    crs = QgsCoordinateReferenceSystem(state["crs"])
    prj.setCrs(crs)
    prj.setTitle(state["name"])
    prj.setFileName(state["qgz"])
    root = prj.layerTreeRoot()
    groups = {}
    order = [g for g, _t in GROUPS]
    by_key = {}
    rank = {k: i for i, k in enumerate(sty.PRIORITY)}
    for d in sorted(state["layers"], key=lambda d: rank.get(d["style"], len(rank))):
        if not d.get("uri"):
            continue
        if d["kind"] == "raster":
            lyr = QgsRasterLayer(d["uri"], d["name"], "gdal")
        else:
            lyr = QgsVectorLayer(d["uri"], d["name"], "ogr")
        if not lyr.isValid():
            continue
        try:
            sty.apply(lyr, d, state)
        except Exception:  # noqa: BLE001 - a style must not lose the layer
            pass
        prj.addMapLayer(lyr, False)
        by_key.setdefault(d["style"], lyr)
        g = d["group"]
        if g not in groups:
            title = tr(dict(GROUPS).get(g, g))
            idx = sum(1 for k in order[:order.index(g)] if k in groups) if g in order else len(groups)
            groups[g] = root.insertGroup(idx, title)
        node = groups[g].addLayer(lyr)
        if d.get("visible") is False:
            node.setItemVisibilityChecked(False)
    # only the study area and one image/terrain backdrop visible at first; tables and admin context collapsed
    for g, node in groups.items():
        node.setExpanded(g in ("area", "osm"))
    if "admin" in groups:
        groups["admin"].setItemVisibilityChecked(False)
    if "rgb" in by_key and "terrain" in groups:
        groups["terrain"].setItemVisibilityChecked(False)
    if "landcover" in groups and ("rgb" in by_key or "dem" in by_key):
        groups["landcover"].setItemVisibilityChecked(False)
    if "population" in groups:
        groups["population"].setItemVisibilityChecked(False)
    area_lyr = by_key.get("area")
    if area_lyr is not None:
        ext = area_lyr.extent()
        ext.scale(1.1)
        from qgis.core import QgsReferencedRectangle

        prj.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(ext, crs))
    layout = None
    if state["options"].get("location_map", True) and area_lyr is not None:
        layout = build_location_layout(prj, area_lyr, by_key.get("country"), by_key.get("admin1"),
                                       by_key.get("region"), backdrop=[by_key.get(k) for k in ("roads", "water")
                                                                       if by_key.get(k)],
                                       names=state.get("country") or {}, study_name=state["name"],
                                       credits=_credits(state))
    prj.writeEntry("Biruni", "/folder-project", True)
    prj.writeEntry("Biruni", "/thesis-starter", json.dumps({"date": state["date"], "crs": state["crs"]}))
    if not prj.write(state["qgz"]):
        raise QgsProcessingException(prj.error())
    state["layout"] = layout.name() if layout is not None else ""
    return prj


def _credits(state):
    s = state.get("sources", {})
    parts = []
    if s.get("geoboundaries"):
        parts.append("geoBoundaries (CC BY 4.0)")
    if s.get("naturalearth"):
        parts.append("Natural Earth")
    if s.get("osm"):
        parts.append("© OpenStreetMap contributors (ODbL)")
    return tr("Data: {d}").format(d=", ".join(parts)) if parts else ""


def build_location_layout(prj, area, country=None, admin1=None, region=None, backdrop=(), names=None,
                          study_name="", credits=""):
    """Chapter 1 location map: three nested map frames (country → region → study area) with labels, north
    arrow, scale bars and extent indicators. Tagged with the thesis figure property."""
    from qgis.core import (QgsLayoutItemLabel, QgsLayoutItemMap, QgsLayoutItemMapOverview, QgsLayoutItemPicture,
                           QgsLayoutMeasurement, QgsLayoutPoint, QgsLayoutSize, QgsPrintLayout, QgsUnitTypes)
    from qgis.PyQt.QtGui import QColor

    from .. import mapsheet

    names = names or {}
    layout = QgsPrintLayout(prj)
    layout.initializeDefaults()  # A4
    layout.setName(tr("Location of the study area"))
    page = layout.pageCollection().page(0)
    from qgis.core import QgsLayoutItemPage

    page.setPageSize("A4", QgsLayoutItemPage.Orientation.Landscape)
    crs = prj.crs()
    area_ext = area.extent()

    def grown(r, f):
        r = QgsRectangle(r)
        r.scale(f)
        return r

    def frame(x, y, w, h, layers, extent, item_id):
        mp = QgsLayoutItemMap(layout)
        mp.setId(item_id)
        mp.attemptMove(QgsLayoutPoint(x, y))
        mp.attemptResize(QgsLayoutSize(w, h))
        mp.setCrs(crs)
        mp.setFrameEnabled(True)
        mp.setFrameStrokeWidth(QgsLayoutMeasurement(0.35))
        mp.setBackgroundColor(QColor("#FFFFFF"))
        mp.setLayers([lyr for lyr in layers if lyr is not None])
        mp.setKeepLayerSet(True)
        layout.addLayoutItem(mp)
        mp.zoomToExtent(extent)
        return mp

    def label(text, x, y, w, size=10, bold=True):
        lab = QgsLayoutItemLabel(layout)
        lab.setText(text)
        lab.setTextFormat(mapsheet._text_format(size, bold))
        layout.addLayoutItem(lab)
        lab.attemptMove(QgsLayoutPoint(x, y))
        lab.attemptResize(QgsLayoutSize(w, 6))
        return lab

    region_ext = region.extent() if region is not None else grown(area_ext, 8)
    country_ext = country.extent() if country is not None else grown(region_ext, 6)
    for r in (region_ext, country_ext):
        if r.isEmpty():
            r.combineExtentWith(area_ext)
    lx, lw, top = 10, 92, 16
    cmap = frame(lx, top, lw, 84, [area, region, admin1, country], grown(country_ext, 1.05), "Country")
    rmap = frame(lx, top + 98, lw, 84, [area, region, admin1, country], grown(region_ext, 1.1), "Region")
    mmap = frame(110, top, 177, 182, list(backdrop) + [area], grown(area_ext, 1.15), "Study area")
    for big, small in ((cmap, rmap), (rmap, mmap)):
        ov = QgsLayoutItemMapOverview(tr("Extent of {n}").format(n=small.id()), big)
        ov.setLinkedMap(small)
        sym = ov.frameSymbol()
        sym.setColor(QColor(220, 30, 30, 60))
        sym.symbolLayer(0).setStrokeColor(QColor("#C81E1E"))
        big.overviews().addOverview(ov)
    country_name = (names.get("name_ar") if _arabic() and names.get("name_ar") else names.get("name")) or tr("Country")
    label(f"(a) {country_name}", lx, top - 7, lw)
    label(f"(b) {names.get('region') or tr('Region')}", lx, top + 91, lw)
    label(f"(c) {study_name or tr('Study area')}", 110, top - 7, 177)
    try:
        mapsheet._add_grid(mmap, _arabic())
    except Exception:  # noqa: BLE001
        pass
    arrow = QgsLayoutItemPicture(layout)
    svg = mapsheet._north_arrow_svg()
    if svg:
        arrow.setPicturePath(svg)
    arrow.setLinkedMap(mmap)
    arrow.setNorthMode(QgsLayoutItemPicture.NorthMode.GridNorth)
    arrow.setId("North arrow")
    layout.addLayoutItem(arrow)
    arrow.attemptMove(QgsLayoutPoint(270, top + 3))
    arrow.attemptResize(QgsLayoutSize(12, 16))
    km = (QgsUnitTypes.DistanceUnit.DistanceKilometers, tr("km"))
    mapsheet._scale_bar(layout, mmap, km if area_ext.width() > 3000 else
                        (QgsUnitTypes.DistanceUnit.DistanceMeters, tr("m")), 113, top + 168, 60, "Scale bar")
    mapsheet._scale_bar(layout, rmap, km, lx + 2, top + 98 + 75, 40, "Scale bar (region)")
    if credits:
        label(credits, 110, 201, 177, size=6, bold=False)
    layout.setCustomProperty(FIGURE_KEY, json.dumps(FIGURE, ensure_ascii=False))
    prj.layoutManager().addLayout(layout)
    return layout


def _arabic():
    from .. import i18n

    return i18n.lang() == "ar"


# ---------------------------------------------------------------------------------------------------
# all in one
# ---------------------------------------------------------------------------------------------------
def run(area, parent, name, options=None, sources=None, feedback=None, context=None):
    state = fetch(area, parent, name, options, sources, feedback, context)
    build_project(state)
    return state


def report_lines(state):
    out = []
    for r in state["results"]:
        mark = "OK" if r["ok"] else ("FAILED" if r["ok"] is False else "SKIPPED")
        out.append(f"{mark}: {title_text(r['title'])}" + (f" - {r['reason']}" if r["reason"] else ""))
    return out


def title_text(title):
    """A source title in the interface language ("OpenStreetMap: <data>" is translated by parts)."""
    head = "OpenStreetMap: "
    if title.startswith(head):
        return head + tr(title[len(head):])
    return tr(title)
