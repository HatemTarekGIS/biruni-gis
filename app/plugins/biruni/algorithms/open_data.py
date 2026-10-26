"""Open data in one step: draw (or take) an area, get the data.

* OpenStreetMap (Overpass API, ODbL): buildings, roads, places, water, land use, railways
* Sentinel-2 satellite imagery (Element 84 Earth Search STAC, Copernicus licence):
  true colour, false colour or NDVI, least-cloudy scene of a period
* Elevation: Copernicus DEM GLO-30 (30 m), optional hillshade
* Land cover: ESA WorldCover 10 m 2021 (CC BY 4.0), with area per class
* Administrative boundaries: geoBoundaries (CC BY 4.0), any country and level

Everything downloads only the requested area. Sources are streamed with GDAL
(/vsicurl/) or fetched through QGIS' network stack (proxy settings apply).
"""
import datetime as dt
import json
import math
import os
import re
import tempfile

from qgis.core import (QgsBlockingNetworkRequest, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
                       QgsFeatureSink, QgsField, QgsFields, QgsProcessing, QgsProcessingException,
                       QgsProcessingParameterBoolean, QgsProcessingParameterEnum, QgsProcessingParameterExtent,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterNumber,
                       QgsProcessingParameterRasterDestination, QgsProcessingParameterString, QgsProject,
                       QgsRectangle, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from ..i18n import tr
from ..version import APP_NAME, VERSION
from .base import BiruniAlgorithm

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
USER_AGENT = f"BiruniGIS/{VERSION} (+desktop GIS)".encode()
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter", "https://overpass-api.de/api/interpreter")
STAC = "https://earth-search.aws.element84.com/v1/search"
DEM_URL = ("https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM/"
           "Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif")
WORLDCOVER_URL = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
                  "ESA_WorldCover_10m_2021_v200_{ns}{lat:02d}{ew}{lon:03d}_Map.tif")
GEOBOUNDARIES = "https://www.geoboundaries.org/api/current/gbOpen/{iso}/ADM{level}/"

WORLDCOVER_CLASSES = [
    (10, "Tree cover", "#006400"), (20, "Shrubland", "#ffbb22"), (30, "Grassland", "#ffff4c"),
    (40, "Cropland", "#f096ff"), (50, "Built-up", "#fa0000"), (60, "Bare / sparse vegetation", "#b4b4b4"),
    (70, "Snow and ice", "#f0f0f0"), (80, "Permanent water bodies", "#0064c8"), (90, "Herbaceous wetland", "#0096a0"),
    (95, "Mangroves", "#00cf75"), (100, "Moss and lichen", "#fae6a0")]
TITLES = [c[1] for c in WORLDCOVER_CLASSES]


# ---- helpers ----------------------------------------------------------------
def http(url, feedback=None, post=None, content_type="application/json"):
    req = QNetworkRequest(QUrl(url))
    req.setRawHeader(b"User-Agent", USER_AGENT)
    br = QgsBlockingNetworkRequest()
    if post is not None:
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, content_type)
        err = br.post(req, post if isinstance(post, bytes) else post.encode("utf-8"), True, feedback)
    else:
        err = br.get(req, True, feedback)
    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise QgsProcessingException(tr("Download failed: {e}").format(e=br.errorMessage()))
    return bytes(br.reply().content())


def bbox_wgs84(alg, parameters, context, name="EXTENT"):
    crs = alg.parameterAsExtentCrs(parameters, name, context)
    rect = alg.parameterAsExtent(parameters, name, context, crs)
    if rect.isEmpty():
        raise QgsProcessingException(tr("Choose an area first (draw it or use the map extent)."))
    t = QgsCoordinateTransform(crs if crs.isValid() else context.project().crs(), WGS84, context.project()
                               or QgsProject.instance())
    r = t.transformBoundingBox(rect)
    return QgsRectangle(max(-180, r.xMinimum()), max(-85, r.yMinimum()), min(180, r.xMaximum()),
                        min(85, r.yMaximum()))


def area_km2(r):
    lat = math.radians((r.yMinimum() + r.yMaximum()) / 2)
    return abs(r.width() * 111.32 * math.cos(lat) * r.height() * 110.57)


def utm_for(r):
    lon, lat = r.center().x(), r.center().y()
    zone = int((lon + 180) // 6) + 1
    return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if lat >= 0 else 32700) + zone}")


def gdal_progress(feedback, start=0, span=100):
    def cb(complete, _msg, _data):
        if feedback is not None:
            feedback.setProgress(start + complete * span)
            return 0 if feedback.isCanceled() else 1
        return 1
    return cb


def _tags(text):
    """OSM 'other_tags' hstore string -> dict."""
    return dict(re.findall(r'"((?:[^"\\]|\\.)*)"=>"((?:[^"\\]|\\.)*)"', text or ""))


# ---- OpenStreetMap -------------------------------------------------------------
OSM_SETS = [
    ("buildings", "Buildings", "multipolygons",
     '(way["building"]({b});relation["building"]({b}););',
     [("building", "building"), ("levels", "building:levels"), ("height", "height"), ("amenity", "amenity"),
      ("shop", "shop"), ("street", "addr:street"), ("house_no", "addr:housenumber")]),
    ("roads", "Roads and streets", "lines", 'way["highway"]({b});',
     [("highway", "highway"), ("ref", "ref"), ("oneway", "oneway"), ("maxspeed", "maxspeed"), ("lanes", "lanes"),
      ("surface", "surface"), ("bridge", "bridge"), ("tunnel", "tunnel")]),
    ("places", "Places and services (POIs)", "points",
     '(node["amenity"]({b});node["shop"]({b});node["tourism"]({b});node["office"]({b});node["healthcare"]({b});'
     'node["leisure"]({b});node["craft"]({b}););',
     [("category", None), ("type", None), ("street", "addr:street"), ("phone", "phone"),
      ("opening_hours", "opening_hours"), ("website", "website")]),
    ("water", "Water (rivers, lakes, canals)", "multipolygons",
     '(way["natural"="water"]({b});relation["natural"="water"]({b});way["waterway"="riverbank"]({b}););',
     [("water", "water"), ("natural", "natural")]),
    ("waterways", "Waterways (lines)", "lines", 'way["waterway"]({b});', [("waterway", "waterway")]),
    ("landuse", "Land use and parks", "multipolygons",
     '(way["landuse"]({b});relation["landuse"]({b});way["leisure"="park"]({b});relation["leisure"="park"]({b}););',
     [("landuse", "landuse"), ("leisure", "leisure")]),
    ("railways", "Railways", "lines", 'way["railway"]({b});', [("railway", "railway"), ("usage", "usage")]),
]
TITLES += [s[1] for s in OSM_SETS]
POI_KEYS = ("amenity", "shop", "tourism", "office", "healthcare", "leisure", "craft")


class DownloadOsm(BiruniAlgorithm):
    ID = "downloadosm"
    NAME = "Download OpenStreetMap Data"
    GROUP = "Open data"
    GROUP_ID = "opendata"
    HELP = ("Downloads OpenStreetMap features of the chosen kind inside an area - buildings, roads, places and "
            "services, water, land use or railways - with their names (also in Arabic and English when mapped) "
            "and the most useful tags as fields. Data © OpenStreetMap contributors, ODbL licence. Keep areas "
            "reasonable (up to a city district for buildings).")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterExtent("EXTENT", tr("Area")))
        self.addParameter(QgsProcessingParameterEnum("DATA", tr("Data"), [tr(s[1]) for s in OSM_SETS],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterFeatureSink("OUTPUT", tr("OpenStreetMap layer")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        key, title, layer_name, query, extra = OSM_SETS[self.parameterAsEnum(parameters, "DATA", context)]
        r = bbox_wgs84(self, parameters, context)
        km2 = area_km2(r)
        limit = 60 if key in ("buildings", "places") else 900
        if km2 > limit:
            raise QgsProcessingException(tr("The area is too large ({a} km²) - choose at most {m} km² for this "
                                            "data.").format(a=f"{km2:,.0f}", m=limit))
        b = f"{r.yMinimum():.6f},{r.xMinimum():.6f},{r.yMaximum():.6f},{r.xMaximum():.6f}"
        # nodes first, everything sorted by id: what the GDAL OSM reader expects
        ql = f"[out:xml][timeout:180];({query.format(b=b)});(._;>;);out body;"
        feedback.pushInfo(tr("Asking OpenStreetMap (Overpass) for {t}…").format(t=tr(title)))
        data = None
        for url in OVERPASS:  # public servers are often busy: try the next one
            if feedback.isCanceled():
                break
            try:
                answer = http(url, feedback, post="data=" + QUrl.toPercentEncoding(ql).data().decode(),
                              content_type="application/x-www-form-urlencoded")
            except QgsProcessingException as e:
                feedback.pushWarning(str(e))
                continue
            head = answer[:2000]
            if b"<osm" in head and b"runtime error" not in answer[-3000:]:
                data = answer
                break
            remark = re.search(rb"<remark>(.*?)</remark>", answer, re.S)
            feedback.pushWarning(f"{url}: " + (remark.group(1).decode("utf-8", "replace").strip() if remark
                                               else head[:200].decode("utf-8", "replace")))
        if data is None or feedback.isCanceled():
            raise QgsProcessingException(tr("OpenStreetMap did not answer. Try again in a minute."))
        tmp = os.path.join(tempfile.mkdtemp(prefix="biruni_osm_"), "data.osm")
        with open(tmp, "wb") as f:
            f.write(data)
        from osgeo import gdal

        gdal.SetConfigOption("OSM_USE_CUSTOM_INDEXING", "NO")
        src = QgsVectorLayer(f"{tmp}|layername={layer_name}", "osm", "ogr")
        if not src.isValid():
            raise QgsProcessingException(tr("The answer could not be read."))
        fields = QgsFields()
        for n in ("osm_id", "name", "name_ar", "name_en") + tuple(e[0] for e in extra):
            fields.append(QgsField(n, QMetaType.Type.QString))
        wkb = {"multipolygons": QgsWkbTypes.Type.MultiPolygon, "lines": QgsWkbTypes.Type.LineString,
               "points": QgsWkbTypes.Type.Point}[layer_name]
        sink, dest = self.parameterAsSink(parameters, "OUTPUT", context, fields, wkb, WGS84)
        src_names = src.fields().names()
        n = 0
        for f in src.getFeatures():
            tags = _tags(f["other_tags"] if "other_tags" in src_names else "")
            for k in src_names:
                if k not in ("other_tags", "osm_id", "osm_way_id") and f[k] not in (None, "") and k not in tags:
                    tags[k] = str(f[k])
            if key == "places" and not any(k in tags for k in POI_KEYS):
                continue
            if key == "water" and layer_name == "multipolygons" and not (tags.get("natural") == "water" or
                                                                        tags.get("waterway") == "riverbank"):
                continue
            if key == "landuse" and not (tags.get("landuse") or tags.get("leisure") == "park"):
                continue
            if key == "buildings" and not tags.get("building"):
                continue
            osm_id = (f["osm_id"] if "osm_id" in src_names and f["osm_id"] else
                      f["osm_way_id"] if "osm_way_id" in src_names else "")
            vals = [str(osm_id or ""), tags.get("name"), tags.get("name:ar"), tags.get("name:en")]
            for fname, tag in extra:
                if fname == "category":
                    vals.append(next((k for k in POI_KEYS if k in tags), None))
                elif fname == "type":
                    vals.append(next((tags[k] for k in POI_KEYS if k in tags), None))
                else:
                    vals.append(tags.get(tag))
            out = QgsFeature(fields)
            out.setGeometry(f.geometry())
            out.setAttributes(vals)
            sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
            n += 1
            if feedback.isCanceled():
                break
        feedback.pushInfo(tr("{n} features downloaded.").format(n=n))
        return {"OUTPUT": dest, "COUNT": n}


# ---- Sentinel-2 ------------------------------------------------------------------
S2_PRODUCTS = ["True colour (RGB)", "False colour (vegetation in red)", "NDVI (vegetation index)"]
TITLES += S2_PRODUCTS


def stac_search(r, days, max_cloud, feedback, end=None):
    end = end or dt.date.today()
    start = end - dt.timedelta(days=days)
    body = {"collections": ["sentinel-2-l2a"], "bbox": [r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum()],
            "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
            "query": {"eo:cloud_cover": {"lt": max_cloud}},
            "sortby": [{"field": "properties.eo:cloud_cover", "direction": "asc"}], "limit": 50}
    data = json.loads(http(STAC, feedback, post=json.dumps(body)).decode("utf-8"))
    return data.get("features", [])


def pick_scenes(items, r):
    """Fewest least-cloudy scenes that cover the area (greedy)."""
    from qgis.core import QgsGeometry

    need = QgsGeometry.fromRect(r)
    chosen = []
    for it in items:
        g = QgsGeometry.fromWkt(_geojson_to_wkt(it["geometry"]))
        if g.isNull() or not g.intersects(need):
            continue
        chosen.append(it)
        need = need.difference(g)
        if need.isEmpty() or need.area() < 1e-9:
            break
    return chosen


def _geojson_to_wkt(g):
    def ring(c):
        return "(" + ",".join(f"{x} {y}" for x, y, *_ in c) + ")"
    if g["type"] == "Polygon":
        return "POLYGON(" + ",".join(ring(c) for c in g["coordinates"]) + ")"
    if g["type"] == "MultiPolygon":
        return "MULTIPOLYGON(" + ",".join("(" + ",".join(ring(c) for c in p) + ")" for p in g["coordinates"]) + ")"
    return ""


def _warp(urls, out, r, crs, res, feedback, resample="bilinear", nodata=0, out_type=None, progress=(0, 100)):
    from osgeo import gdal

    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF,.tiff")
    t = QgsCoordinateTransform(WGS84, crs, QgsProject.instance())
    b = t.transformBoundingBox(r)
    opts = gdal.WarpOptions(format="GTiff", outputBounds=(b.xMinimum(), b.yMinimum(), b.xMaximum(), b.yMaximum()),
                            dstSRS=crs.authid(), xRes=res, yRes=res, resampleAlg=resample, srcNodata=nodata,
                            dstNodata=nodata, multithread=True, outputType=out_type or gdal.GDT_Unknown,
                            creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
                            callback=gdal_progress(feedback, *progress))
    ds = gdal.Warp(out, ["/vsicurl/" + u for u in urls], options=opts)
    if ds is None:
        raise QgsProcessingException(tr("The imagery could not be read: {e}").format(e=gdal.GetLastErrorMsg()))
    ds = None


class DownloadSentinel2(BiruniAlgorithm):
    ID = "downloadsentinel2"
    NAME = "Download Satellite Image (Sentinel-2)"
    GROUP = "Open data"
    GROUP_ID = "opendata"
    HELP = ("Downloads the clearest Sentinel-2 satellite image (10 m) of the chosen area from the last days - "
            "true colour, false colour or the NDVI vegetation index - cut to the area. Free Copernicus data "
            "(Earth Search by Element 84). Clouds can still appear: raise the period or lower the cloud limit.")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterExtent("EXTENT", tr("Area")))
        self.addParameter(QgsProcessingParameterEnum("PRODUCT", tr("Image"), [tr(p) for p in S2_PRODUCTS],
                                                     defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber("DAYS", tr("Look back (days)"),
                                                       QgsProcessingParameterNumber.Type.Integer, 60, False, 5, 3650))
        self.addParameter(QgsProcessingParameterNumber("CLOUD", tr("Maximum cloud cover (%)"),
                                                       QgsProcessingParameterNumber.Type.Integer, 15, False, 0, 100))
        self.addParameter(QgsProcessingParameterNumber("RESOLUTION", tr("Pixel size (m)"),
                                                       QgsProcessingParameterNumber.Type.Double, 10, False, 10, 1000))
        self.addParameter(QgsProcessingParameterRasterDestination("OUTPUT", tr("Satellite image")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        r = bbox_wgs84(self, parameters, context)
        product = self.parameterAsEnum(parameters, "PRODUCT", context)
        res = self.parameterAsDouble(parameters, "RESOLUTION", context)
        km2 = area_km2(r)
        pixels = km2 * 1e6 / (res * res)
        if pixels > 400e6:
            res = math.ceil(math.sqrt(km2 * 1e6 / 400e6) / 10) * 10
            feedback.pushWarning(tr("Large area: the pixel size was raised to {r} m.").format(r=res))
        out = self.parameterAsOutputLayer(parameters, "OUTPUT", context)
        items = stac_search(r, self.parameterAsInt(parameters, "DAYS", context),
                            self.parameterAsInt(parameters, "CLOUD", context), feedback)
        scenes = pick_scenes(items, r)
        if not scenes:
            raise QgsProcessingException(tr("No clear image was found. Look back more days or allow more clouds."))
        for s in scenes:
            p = s["properties"]
            feedback.pushInfo(tr("Scene {id}: {date}, clouds {c}%").format(
                id=s["id"], date=p.get("datetime", "")[:10], c=f"{p.get('eo:cloud_cover', 0):.1f}"))
        crs = utm_for(r)
        order = list(reversed(scenes))  # the best scene is drawn last (on top)
        if product in (0, 1):
            bands = ("red", "green", "blue") if product == 0 else ("nir", "red", "green")
            tmp = tempfile.mkdtemp(prefix="biruni_s2_")
            parts = []
            for k, band in enumerate(bands):
                part = os.path.join(tmp, f"{band}.tif")
                _warp([s["assets"][band]["href"] for s in order], part, r, crs, res, feedback,
                      progress=(k * 30, 30))
                parts.append(part)
            _stack_rgb(parts, out)
        else:
            tmp = tempfile.mkdtemp(prefix="biruni_s2_")
            red, nir = os.path.join(tmp, "red.tif"), os.path.join(tmp, "nir.tif")
            _warp([s["assets"]["red"]["href"] for s in order], red, r, crs, res, feedback, progress=(0, 45))
            _warp([s["assets"]["nir"]["href"] for s in order], nir, r, crs, res, feedback, progress=(45, 45))
            _ndvi(red, nir, out)
            _ndvi_style(out)
        feedback.setProgress(100)
        meta = {"scenes": [s["id"] for s in scenes]}
        feedback.pushInfo(json.dumps(meta))
        return {"OUTPUT": out}


def _stack_rgb(parts, out):
    """3 single-band reflectance rasters -> an 8-bit RGB image stretched for display."""
    import numpy as np
    from osgeo import gdal

    srcs = [gdal.Open(p) for p in parts]
    first = srcs[0]
    drv = gdal.GetDriverByName("GTiff")
    dst = drv.Create(out, first.RasterXSize, first.RasterYSize, 3, gdal.GDT_Byte,
                     ["COMPRESS=DEFLATE", "TILED=YES", "PHOTOMETRIC=RGB"])
    dst.SetGeoTransform(first.GetGeoTransform())
    dst.SetProjection(first.GetProjection())
    for k, ds in enumerate(srcs):
        a = ds.GetRasterBand(1).ReadAsArray().astype("float32")
        valid = a > 0
        if valid.any():
            lo, hi = np.percentile(a[valid], (2, 98))
        else:
            lo, hi = 0, 1
        scaled = np.clip((a - lo) / max(hi - lo, 1) * 254 + 1, 1, 255)
        scaled[~valid] = 0
        band = dst.GetRasterBand(k + 1)
        band.WriteArray(scaled.astype("uint8"))
        band.SetNoDataValue(0)
    dst = None


def _ndvi(red, nir, out):
    import numpy as np
    from osgeo import gdal

    r_ds, n_ds = gdal.Open(red), gdal.Open(nir)
    r = r_ds.GetRasterBand(1).ReadAsArray().astype("float32")
    n = n_ds.GetRasterBand(1).ReadAsArray().astype("float32")
    with np.errstate(divide="ignore", invalid="ignore"):
        v = (n - r) / (n + r)
    v[(r <= 0) | (n <= 0) | ~np.isfinite(v)] = -9999
    dst = gdal.GetDriverByName("GTiff").Create(out, r_ds.RasterXSize, r_ds.RasterYSize, 1, gdal.GDT_Float32,
                                               ["COMPRESS=DEFLATE", "TILED=YES"])
    dst.SetGeoTransform(r_ds.GetGeoTransform())
    dst.SetProjection(r_ds.GetProjection())
    b = dst.GetRasterBand(1)
    b.WriteArray(v)
    b.SetNoDataValue(-9999)
    dst = None


def _write_default_style(path, renderer_fn):
    """Save a default style (<file>.qml) so the raster opens styled."""
    from qgis.core import QgsMapLayer, QgsRasterLayer

    lyr = QgsRasterLayer(path, "tmp")
    if lyr.isValid():
        lyr.setRenderer(renderer_fn(lyr))
        lyr.saveDefaultStyle(QgsMapLayer.StyleCategory.AllStyleCategories)


def _ndvi_style(path):
    from qgis.core import QgsColorRampShader, QgsRasterShader, QgsSingleBandPseudoColorRenderer
    from qgis.PyQt.QtGui import QColor

    def make(lyr):
        ramp = QgsColorRampShader(-0.2, 0.9)
        ramp.setColorRampType(QgsColorRampShader.Type.Interpolated)
        ramp.setColorRampItemList([QgsColorRampShader.ColorRampItem(v, QColor(c), lab) for v, c, lab in (
            (-0.2, "#2b83ba", tr("Water")), (0.05, "#d7c49e", tr("Bare soil / built-up")),
            (0.2, "#f4f070", tr("Sparse vegetation")), (0.45, "#91cf60", tr("Moderate vegetation")),
            (0.9, "#1a9641", tr("Dense vegetation")))])
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(ramp)
        return QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, shader)

    _write_default_style(path, make)


# ---- elevation ------------------------------------------------------------------
def dem_tiles(r):
    urls = []
    for lat in range(math.floor(r.yMinimum()), math.ceil(r.yMaximum())):
        for lon in range(math.floor(r.xMinimum()), math.ceil(r.xMaximum())):
            urls.append(DEM_URL.format(ns="N" if lat >= 0 else "S", lat=abs(lat), ew="E" if lon >= 0 else "W",
                                       lon=abs(lon)))
    return urls


def _existing(urls, feedback):
    from osgeo import gdal

    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    ok = []
    for u in urls:
        if feedback.isCanceled():
            break
        ds = gdal.Open("/vsicurl/" + u)
        if ds is not None:
            ok.append(u)
        ds = None
    return ok


class DownloadDem(BiruniAlgorithm):
    ID = "downloaddem"
    NAME = "Download Elevation (DEM 30 m)"
    GROUP = "Open data"
    GROUP_ID = "opendata"
    HELP = ("Downloads ground elevation for the chosen area from the Copernicus DEM GLO-30 (30 m, worldwide) in "
            "metres, in the UTM zone of the area, ready for slope, contours, profiles and viewsheds. Optionally "
            "also a hillshade. © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, provided "
            "under COPERNICUS by the European Union and ESA.")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterExtent("EXTENT", tr("Area")))
        self.addParameter(QgsProcessingParameterBoolean("HILLSHADE", tr("Also make a hillshade"), True))
        self.addParameter(QgsProcessingParameterRasterDestination("OUTPUT", tr("Elevation")))
        self.addParameter(QgsProcessingParameterRasterDestination("HILLSHADE_OUTPUT", tr("Hillshade"),
                                                                  optional=True, createByDefault=True))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        from osgeo import gdal

        r = bbox_wgs84(self, parameters, context)
        if area_km2(r) > 40000:
            raise QgsProcessingException(tr("The area is too large - choose at most 40,000 km²."))
        urls = _existing(dem_tiles(r), feedback)
        if not urls:
            raise QgsProcessingException(tr("There is no elevation data here (sea or outside the coverage)."))
        out = self.parameterAsOutputLayer(parameters, "OUTPUT", context)
        _warp(urls, out, r, utm_for(r), 30, feedback, nodata=-32767, out_type=gdal.GDT_Float32, progress=(0, 85))
        results = {"OUTPUT": out}
        if self.parameterAsBoolean(parameters, "HILLSHADE", context):
            hs = self.parameterAsOutputLayer(parameters, "HILLSHADE_OUTPUT", context)
            if hs:
                gdal.DEMProcessing(hs, out, "hillshade", options=gdal.DEMProcessingOptions(
                    multiDirectional=True, computeEdges=True, creationOptions=["COMPRESS=DEFLATE"]))
                results["HILLSHADE_OUTPUT"] = hs
        feedback.setProgress(100)
        return results


# ---- land cover -------------------------------------------------------------------
def worldcover_tiles(r):
    urls = []
    for lat in range(math.floor(r.yMinimum() / 3) * 3, math.ceil(r.yMaximum() / 3) * 3, 3):
        for lon in range(math.floor(r.xMinimum() / 3) * 3, math.ceil(r.xMaximum() / 3) * 3, 3):
            urls.append(WORLDCOVER_URL.format(ns="N" if lat >= 0 else "S", lat=abs(lat),
                                              ew="E" if lon >= 0 else "W", lon=abs(lon)))
    return urls


class DownloadLandCover(BiruniAlgorithm):
    ID = "downloadlandcover"
    NAME = "Download Land Cover (10 m)"
    GROUP = "Open data"
    GROUP_ID = "opendata"
    HELP = ("Downloads the ESA WorldCover 2021 land cover map (10 m) of the chosen area - trees, shrubs, grass, "
            "crops, built-up, bare land, water, wetland… - styled with its legend, and reports the area of each "
            "class. © ESA WorldCover project 2021, CC BY 4.0.")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterExtent("EXTENT", tr("Area")))
        self.addParameter(QgsProcessingParameterRasterDestination("OUTPUT", tr("Land cover")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        import numpy as np
        from osgeo import gdal

        r = bbox_wgs84(self, parameters, context)
        if area_km2(r) > 20000:
            raise QgsProcessingException(tr("The area is too large - choose at most 20,000 km²."))
        urls = _existing(worldcover_tiles(r), feedback)
        if not urls:
            raise QgsProcessingException(tr("There is no land cover data here."))
        out = self.parameterAsOutputLayer(parameters, "OUTPUT", context)
        _warp(urls, out, r, utm_for(r), 10, feedback, resample="near", nodata=0, out_type=gdal.GDT_Byte,
              progress=(0, 90))
        ds = gdal.Open(out)
        a = ds.GetRasterBand(1).ReadAsArray()
        ds = None
        values, counts = np.unique(a[a > 0], return_counts=True)
        names = {v: n for v, n, _c in WORLDCOVER_CLASSES}
        areas = {}
        for v, c in zip(values.tolist(), counts.tolist()):
            areas[tr(names.get(v, str(v)))] = round(c * 100 / 1e6, 4)  # 10 m pixels -> km²
            feedback.pushInfo(f"{tr(names.get(v, str(v)))}: {c * 100 / 1e6:,.3f} km²")
        _worldcover_style(out)
        return {"OUTPUT": out, "AREAS_KM2": areas}


def _worldcover_style(path):
    from qgis.core import QgsPalettedRasterRenderer
    from qgis.PyQt.QtGui import QColor

    def make(lyr):
        classes = [QgsPalettedRasterRenderer.Class(v, QColor(c), tr(n)) for v, n, c in WORLDCOVER_CLASSES]
        return QgsPalettedRasterRenderer(lyr.dataProvider(), 1, classes)

    _write_default_style(path, make)


# ---- administrative boundaries ------------------------------------------------------
class DownloadBoundaries(BiruniAlgorithm):
    ID = "downloadboundaries"
    NAME = "Download Administrative Boundaries"
    GROUP = "Open data"
    GROUP_ID = "opendata"
    HELP = ("Downloads the administrative boundaries of a country (geoBoundaries, CC BY 4.0): level 0 is the "
            "country, 1 the first level (governorates, provinces, regions…), 2 the next one, and so on. Leave the "
            "country code empty to use the country in the middle of the chosen area.")
    LEVELS = ["0 - Country", "1 - First level (provinces / governorates)", "2 - Second level (districts)",
              "3 - Third level"]

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterString("COUNTRY", tr("Country code (ISO, e.g. EGY, SAU, FRA)"),
                                                       optional=True))
        self.addParameter(QgsProcessingParameterExtent("EXTENT", tr("Area"), optional=True))
        self.addParameter(QgsProcessingParameterEnum("LEVEL", tr("Level"), [tr(x) for x in self.LEVELS],
                                                     defaultValue=1))
        self.addParameter(QgsProcessingParameterFeatureSink("OUTPUT", tr("Boundaries"),
                                                            QgsProcessing.SourceType.TypeVectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        iso = (self.parameterAsString(parameters, "COUNTRY", context) or "").strip().upper()
        if not iso:
            if not parameters.get("EXTENT"):
                raise QgsProcessingException(tr("Enter a country code or choose an area."))
            from ..places import country_iso3

            c = bbox_wgs84(self, parameters, context).center()
            iso = country_iso3(c.x(), c.y()) or ""
            if not iso:
                raise QgsProcessingException(tr("No country was found in the middle of the area."))
        level = self.parameterAsEnum(parameters, "LEVEL", context)
        info = None
        for lv in range(level, -1, -1):
            try:
                info = json.loads(http(GEOBOUNDARIES.format(iso=iso, level=lv), feedback).decode("utf-8"))
                if lv != level:
                    feedback.pushWarning(tr("Level {a} is not available for {c}; level {b} was used.").format(
                        a=level, b=lv, c=iso))
                break
            except (QgsProcessingException, ValueError):
                continue
        if not info:
            raise QgsProcessingException(tr("No boundaries were found for {c}.").format(c=iso))
        url = info.get("gjDownloadURL") or info.get("simplifiedGeometryGeoJSON")
        feedback.pushInfo(f"{info.get('boundaryName', iso)} · {info.get('boundaryType', '')} · "
                          f"{info.get('boundarySource', '')} · {info.get('boundaryLicense', '')}")
        tmp = os.path.join(tempfile.mkdtemp(prefix="biruni_gb_"), "boundaries.geojson")
        with open(tmp, "wb") as f:
            f.write(http(url, feedback))
        src = QgsVectorLayer(tmp, "gb", "ogr")
        if not src.isValid():
            raise QgsProcessingException(tr("The answer could not be read."))
        sink, dest = self.parameterAsSink(parameters, "OUTPUT", context, src.fields(), QgsWkbTypes.Type.MultiPolygon,
                                          src.crs())
        n = 0
        for f in src.getFeatures():
            g = f.geometry()
            g.convertToMultiType()
            f.setGeometry(g)
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
            n += 1
        return {"OUTPUT": dest, "COUNT": n, "COUNTRY": iso}
