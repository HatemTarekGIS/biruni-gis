"""Overture Maps in one step: buildings, places, roads, addresses, land use,
water and administrative areas for an area, with Arabic and English names.

Overture publishes each release as GeoParquet files on AWS S3 (open, no
account) and a STAC catalogue that gives the bounding box of every file. Only
the files that touch the area are opened, and GDAL reads just the row groups
inside it (GeoParquet 1.1 bbox covering), so a city district takes seconds.
Data © Overture Maps Foundation and its sources (ODbL / CDLA, see the release).
"""
import json

from qgis.core import (Qgis, QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsGeometry, QgsProcessingException,
                       QgsProcessingParameterEnum, QgsProcessingParameterExtent, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterNumber, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm
from .open_data import WGS84, area_km2, bbox_wgs84, http

STAC_ROOT = "https://stac.overturemaps.org"
GDAL_OPTIONS = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "GDAL_HTTP_MAX_RETRY": "3",
                "GDAL_HTTP_RETRY_DELAY": "2", "AWS_NO_SIGN_REQUEST": "YES", "CPL_VSIL_CURL_CACHE_SIZE": "67108864"}

S = QMetaType.Type.QString
D = QMetaType.Type.Double
I = QMetaType.Type.Int  # noqa: E741

# (key, title, theme, type, geometry, area limit km², [(field, type, [source columns, first found wins])])
DATASETS = [
    ("buildings", "Buildings", "buildings", "building", "polygon", 400, [
        ("subtype", S, ["subtype"]), ("class", S, ["class"]), ("height", D, ["height"]),
        ("floors", I, ["num_floors"]), ("roof_shape", S, ["roof_shape"])]),
    ("places", "Places and services (POIs)", "places", "place", "point", 2500, [
        ("category", S, ["basic_category", "taxonomy.primary", "categories.primary"]),
        ("confidence", D, ["confidence"]), ("phone", S, ["phones"]), ("website", S, ["websites"]),
        ("brand", S, ["brand.names.primary"]), ("address", S, ["addresses"])]),
    ("roads", "Roads and paths", "transportation", "segment", "line", 900, [
        ("subtype", S, ["subtype"]), ("class", S, ["class"]), ("subclass", S, ["subclass"])]),
    ("addresses", "Addresses", "addresses", "address", "point", 900, [
        ("number", S, ["number"]), ("street", S, ["street"]), ("unit", S, ["unit"]), ("postcode", S, ["postcode"]),
        ("country", S, ["country"])]),
    ("landuse", "Land use", "base", "land_use", "polygon", 2500, [
        ("subtype", S, ["subtype"]), ("class", S, ["class"])]),
    ("water", "Water", "base", "water", "polygon", 2500, [
        ("subtype", S, ["subtype"]), ("class", S, ["class"])]),
    ("divisions", "Administrative areas", "divisions", "division_area", "polygon", 250000, [
        ("subtype", S, ["subtype"]), ("class", S, ["class"]), ("country", S, ["country"]),
        ("region", S, ["region"])]),
]
# spelled out so scripts/extract_strings.py collects them for translation
TITLES = ["Buildings", "Places and services (POIs)", "Roads and paths", "Addresses", "Land use", "Water",
          "Administrative areas"]
assert TITLES == [d[1] for d in DATASETS]
WKB = {"polygon": QgsWkbTypes.Type.MultiPolygon, "line": QgsWkbTypes.Type.MultiLineString,
       "point": QgsWkbTypes.Type.Point}


# ---- catalogue ---------------------------------------------------------------------
def _json(url, feedback=None):
    return json.loads(http(url, feedback).decode("utf-8"))


def latest_release(feedback=None):
    cat = _json(f"{STAC_ROOT}/catalog.json", feedback)
    rel = cat.get("latest")
    if not rel:  # older catalogues: the child marked latest, else the last one
        kids = [lk for lk in cat.get("links", []) if lk.get("rel") == "child"]
        best = next((lk for lk in kids if lk.get("latest")), kids[-1] if kids else None)
        rel = best["href"].rstrip("/").split("/")[-2] if best else None
    if not rel:
        raise QgsProcessingException(tr("The Overture catalogue could not be read."))
    return rel


def intersecting_files(boxes, bbox):
    """Indexes of the per-file boxes ([w, s, e, n]) that touch bbox."""
    w, s, e, n = bbox
    return [i for i, b in enumerate(boxes) if not (b[2] < w or b[0] > e or b[3] < s or b[1] > n)]


def file_urls(release, theme, typ, bbox, feedback=None):
    """HTTPS URLs of the GeoParquet files of one Overture type that touch bbox."""
    base = f"{STAC_ROOT}/{release}/{theme}/{typ}"
    col = _json(f"{base}/collection.json", feedback)
    boxes = col["extent"]["spatial"]["bbox"][1:]  # [0] is the whole collection, then one per file
    urls = []
    for i in intersecting_files(boxes, bbox):
        if feedback is not None and feedback.isCanceled():
            break
        item = _json(f"{base}/{i:05d}/{i:05d}.json", feedback)
        assets = item.get("assets", {})
        href = (assets.get("aws") or next(iter(assets.values()), {})).get("href")
        if href:
            urls.append(href)
    return urls


# ---- reading -----------------------------------------------------------------------
def _names(value):
    """names.common (JSON map of language -> name) -> dict."""
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _first_text(value):
    """A list/JSON list cell (phones, websites, addresses) -> its first value as text."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None
    s = str(value)
    if s.startswith(("[", "{")):
        try:
            data = json.loads(s)
        except ValueError:
            return s
        if isinstance(data, list):
            data = data[0] if data else None
        if isinstance(data, dict):
            parts = [str(data[k]) for k in ("freeform", "locality", "region", "country") if data.get(k)]
            return ", ".join(parts) or None
        return None if data is None else str(data)
    return s


def read_features(paths, bbox, dataset, limit, feedback=None):
    """Yield (QgsGeometry, [id, name, name_ar, name_en, extra...]) for features of `paths` (GDAL-readable
    GeoParquet files, local or /vsicurl/) inside bbox (WGS 84)."""
    from osgeo import gdal, ogr

    key, _title, _theme, _typ, kind, _limit, extra = dataset
    rect = QgsGeometry.fromWkt(f"POLYGON(({bbox[0]} {bbox[1]},{bbox[2]} {bbox[1]},{bbox[2]} {bbox[3]},"
                               f"{bbox[0]} {bbox[3]},{bbox[0]} {bbox[1]}))")
    want = {"polygon": Qgis.GeometryType.Polygon, "line": Qgis.GeometryType.Line,
            "point": Qgis.GeometryType.Point}[kind]
    n = 0
    with gdal.config_options(GDAL_OPTIONS):
        for p in paths:
            if feedback is not None and feedback.isCanceled():
                return
            src = p if p.startswith("/vsi") or not p.startswith("http") else "/vsicurl/" + p
            ds = ogr.Open(src)
            if ds is None:
                if feedback is not None:
                    feedback.pushWarning(tr("Could not open {p}").format(p=p))
                continue
            lyr = ds.GetLayer(0)
            defn = lyr.GetLayerDefn()
            have = {defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())}
            cols = [next((c for c in sources if c in have), None) for _f, _t, sources in extra]
            lyr.SetSpatialFilterRect(*bbox)
            for f in lyr:
                g = f.GetGeometryRef()
                if g is None:
                    continue
                qg = QgsGeometry()
                qg.fromWkb(bytes(g.ExportToIsoWkb()))
                if qg.isNull() or qg.type() != want:
                    continue
                if not qg.intersects(rect):
                    continue
                names = _names(f.GetField("names.common")) if "names.common" in have else {}
                primary = f.GetField("names.primary") if "names.primary" in have else None
                vals = [f.GetField("id") if "id" in have else None, primary,
                        names.get("ar") or (primary if _is_arabic(primary) else None), names.get("en")]
                for (fname, ftype, _src), col in zip(extra, cols):
                    v = None if col is None else f.GetField(col)
                    if ftype == S:
                        v = _first_text(v)
                    vals.append(v)
                yield qg, vals
                n += 1
                if n >= limit:
                    return
            ds = None  # noqa: F841 - close the file before the next one


def _is_arabic(text):
    return bool(text) and any("؀" <= ch <= "ۿ" for ch in str(text))


def output_fields(dataset):
    fields = QgsFields()
    for name in ("overture_id", "name", "name_ar", "name_en"):
        fields.append(QgsField(name, S))
    for fname, ftype, _src in dataset[6]:
        fields.append(QgsField(fname, ftype))
    return fields


class DownloadOverture(BiruniAlgorithm):
    ID = "downloadoverture"
    NAME = "Download Overture Maps data"
    GROUP = "Open data"
    GROUP_ID = "opendata"
    HELP = ("Downloads Overture Maps features inside an area - buildings (with height and floors where known), "
            "places and services, roads and paths, addresses, land use, water or administrative areas - with "
            "their names in Arabic and English when available. Overture merges OpenStreetMap, Microsoft and Google "
            "building footprints, Meta and Microsoft places and other open sources, so it often has buildings "
            "and shops that are missing elsewhere. Only the needed parts of the latest release are read. Data © "
            "Overture Maps Foundation and contributors; check the licence of each theme (ODbL or CDLA).")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterExtent("EXTENT", tr("Area")))
        self.addParameter(QgsProcessingParameterEnum("DATA", tr("Data"), [tr(t) for t in TITLES], defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber("MAX_FEATURES", tr("At most this many features"),
                                                       defaultValue=200000, minValue=1, maxValue=5000000))
        self.addParameter(QgsProcessingParameterFeatureSink("OUTPUT", tr("Overture layer")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        ds = DATASETS[self.parameterAsEnum(parameters, "DATA", context)]
        r = bbox_wgs84(self, parameters, context)
        km2 = area_km2(r)
        if km2 > ds[5]:
            raise QgsProcessingException(tr("The area is too large ({a} km²) - choose at most {m} km² for this "
                                            "data.").format(a=f"{km2:,.0f}", m=f"{ds[5]:,}"))
        bbox = (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())
        limit = self.parameterAsInt(parameters, "MAX_FEATURES", context)
        feedback.pushInfo(tr("Finding the latest Overture release…"))
        release = latest_release(feedback)
        urls = file_urls(release, ds[2], ds[3], bbox, feedback)
        feedback.pushInfo(tr("Release {r}: {n} file(s) cover this area.").format(r=release, n=len(urls)))
        if not urls:
            raise QgsProcessingException(tr("Overture has no {t} here.").format(t=tr(ds[1])))
        fields = output_fields(ds)
        sink, dest = self.parameterAsSink(parameters, "OUTPUT", context, fields, WKB[ds[4]], WGS84)
        n = 0
        for k, url in enumerate(urls):
            feedback.setProgress(100 * k / len(urls))
            for g, vals in read_features([url], bbox, ds, limit - n, feedback):
                if ds[4] != "point":
                    g.convertToMultiType()
                out = QgsFeature(fields)
                out.setGeometry(g)
                out.setAttributes(vals)
                sink.addFeature(out, QgsFeatureSink.Flag.FastInsert)
                n += 1
            if n >= limit or feedback.isCanceled():
                break
        if n >= limit:
            feedback.pushWarning(tr("Stopped at {n} features - raise the limit or choose a smaller area.").format(n=n))
        feedback.pushInfo(tr("{n} features downloaded.").format(n=n))
        return {"OUTPUT": dest, "COUNT": n, "RELEASE": release}
