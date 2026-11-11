"""Automatic methodology chapter (سجل المنهجية).

Collects what was done in the current project - the data used (with its origin
and licence), the coordinate system, the Processing tools run with their
important settings, manual edits and the print layouts made - and writes it as
an academic methodology chapter in Arabic or English, with a workflow diagram,
a data sources table and a references list.

Where the facts come from:
  * the project's layers (provider, source, CRS, resolution, feature counts,
    metadata) and print layouts;
  * Biruni's activity log (activity.py): when layers were added, tools run and
    features edited;
  * a light recorder in this module: every Processing run (from the toolbox
    dialogs through QGIS' history provider, and from scripts through
    ``processing.run``) with its parameters, inputs, outputs and success.
    The runs are kept in the project itself (``biruni/methodology_runs``) so
    they travel with the .qgz.

Public API:
  methodology_steps(project=None) -> list[dict]
  methodology_text(lang="ar", steps=None) -> (title, [paragraphs])
  methodology_document(lang="ar", steps=None) -> dict (title, paragraphs, sources, references, steps)
  workflow_image(path, lang="ar", steps=None) -> path   (.png or .svg)
  export_methodology(path, lang="ar", steps=None) -> [written files]
  install_recorder() / uninstall_recorder()
"""
import datetime as dt
import html
import json
import os
import re
import threading
import time

from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsMapLayer, QgsProcessingContext,
                       QgsProcessingFeatureSourceDefinition, QgsProcessingOutputLayerDefinition, QgsProject,
                       QgsRasterLayer, QgsUnitTypes, QgsVectorLayer)

SCOPE = "biruni"
KEY = "methodology_runs"
MAX_RUNS = 400
# tools that describe the work rather than being part of it
IGNORED_ALGS = {"biruni:thesismethodology"}
EARLY = ""            # sort key for things that happened before everything else
LATE = "9999-12-31"   # ... and after


# =============================================================================
# Recorder
# =============================================================================
def _now():
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _main_thread():
    app = QgsApplication.instance()
    if app is None:
        return threading.current_thread() is threading.main_thread()
    from qgis.PyQt.QtCore import QThread

    return QThread.currentThread() == app.thread()


def _norm_path(s):
    s = str(s or "").split("|")[0].strip()
    if s.startswith("file:///"):
        s = s[8:]
    return os.path.normcase(os.path.normpath(s)) if s and ("/" in s or "\\" in s) else s


def _short(v, n=400):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


def _layer_ref(value, prj):
    """(display name, keys) of a layer-like parameter value."""
    if isinstance(value, QgsProcessingFeatureSourceDefinition):
        value = value.source.staticValue() if hasattr(value.source, "staticValue") else value.source
    if isinstance(value, QgsProcessingOutputLayerDefinition):
        value = value.sink.staticValue() if hasattr(value.sink, "staticValue") else value.sink
    if isinstance(value, QgsMapLayer):
        return value.name(), [value.id(), _norm_path(value.source())]
    if isinstance(value, dict):  # asMap() form of a feature source definition
        value = value.get("source", value.get("sink", "")) or ""
        if isinstance(value, dict):
            value = value.get("val", "") or ""
    s = str(value or "")
    if not s:
        return "", []
    lyr = prj.mapLayer(s) if prj else None
    if lyr is None and prj:
        for cand in prj.mapLayersByName(s):
            lyr = cand
            break
    if lyr is None and prj:
        ns = _norm_path(s)
        for cand in prj.mapLayers().values():
            if _norm_path(cand.source()) == ns:
                lyr = cand
                break
    if lyr is not None:
        return lyr.name(), [lyr.id(), _norm_path(lyr.source())]
    if s.startswith("memory:") or s == "TEMPORARY_OUTPUT":
        return "", [s]
    base = os.path.basename(s.split("|")[0])
    return os.path.splitext(base)[0] or s, [_norm_path(s)]


GENERIC_NAMES = {"output", "", "temporary_output", "memory"}
LAYER_PARAM_TYPES = {"source", "vector", "raster", "layer", "multilayer", "mesh", "pointcloud", "vectortile",
                     "annotation"}


def _ptype(p):
    try:
        return p.type()
    except Exception:
        return ""


def _record_from(alg, parameters, results, ok, via, raw_map=None):
    """A JSON-friendly record of one Processing run."""
    prj = QgsProject.instance()
    ctx = QgsProcessingContext()
    ctx.setProject(prj)
    as_map = raw_map
    if as_map is None:
        try:
            as_map = (alg.asMap(parameters, ctx) or {}).get("inputs", {})
        except Exception:
            as_map = {}
    params, layers, keys_in, outputs, key_names, units, layer_keys = {}, {}, [], {}, {}, {}, {}
    for p in alg.parameterDefinitions():
        name = p.name()
        if name not in parameters and name not in as_map:
            continue
        value = parameters.get(name, as_map.get(name))
        if p.isDestination():
            continue
        if _ptype(p) in LAYER_PARAM_TYPES:
            vals = value if isinstance(value, (list, tuple)) else [value]
            names, first_keys = [], []
            for v in vals:
                n, k = _layer_ref(v, prj)
                if n or k:
                    names.append(n)
                    first_keys.append(k[0] if k else "")
                    if n and n.lower() not in GENERIC_NAMES:
                        for key in k:
                            key_names[key] = n
                keys_in += k
            layers[name] = names
            layer_keys[name] = first_keys
        elif _ptype(p) == "distance":
            parent = p.parentParameterName()
            pv = parameters.get(parent, as_map.get(parent)) if parent else None
            crs = _value_crs(pv, prj) if pv is not None else None
            if crs is not None and crs.isValid():
                units[name] = QgsUnitTypes.encodeUnit(crs.mapUnits())
        v = as_map.get(name, value)
        if isinstance(v, (QgsMapLayer,)):
            v = v.id()
        try:
            json.dumps(v)
        except TypeError:
            v = str(v)
        params[name] = v if not isinstance(v, str) else _short(v)
    for k, v in (results or {}).items():
        if k == "CHILD_INPUTS":
            continue
        d = alg.outputDefinition(k)
        if d is None or not d.type() in ("outputVector", "outputRaster", "outputLayer", "outputMultilayers",
                                         "outputPointCloud", "outputVectorTile", "outputFile", "outputHtml",
                                         "outputFolder"):
            continue
        vals = v if isinstance(v, (list, tuple)) else [v]
        for one in vals:
            n, keys = _layer_ref(one, prj)
            outputs.setdefault(k, {"name": n or d.description(), "desc": d.description(), "keys": []})
            outputs[k]["keys"] += keys
    return {"ts": _now(), "alg": alg.id(), "ok": ok, "via": via, "params": params, "layers": layers,
            "keys_in": sorted(set(x for x in keys_in if x)), "outputs": outputs, "names": key_names,
            "units": units, "layer_keys": layer_keys}


def _value_crs(value, prj):
    """CRS of a layer-like parameter value (layer object, project layer id/name/source, or file path)."""
    if isinstance(value, QgsProcessingFeatureSourceDefinition):
        value = value.source.staticValue() if hasattr(value.source, "staticValue") else value.source
    if isinstance(value, QgsMapLayer):
        return value.crs()
    if isinstance(value, dict):
        value = value.get("source", "") or ""
        if isinstance(value, dict):
            value = value.get("val", "") or ""
    _n, keys = _layer_ref(value, prj)
    for k in keys:
        lyr = prj.mapLayer(k)
        if lyr is None:
            for cand in prj.mapLayers().values():
                if _norm_path(cand.source()) == k:
                    lyr = cand
                    break
        if lyr is not None:
            return lyr.crs()
    path = str(value or "").split("|")[0]
    if path and os.path.exists(path):
        lyr = QgsRasterLayer(path, "x")
        if not lyr.isValid():
            lyr = QgsVectorLayer(str(value), "x", "ogr")
        if lyr.isValid():
            return lyr.crs()
    return None


def load_runs(prj=None):
    prj = prj or QgsProject.instance()
    txt, _ok = prj.readEntry(SCOPE, KEY, "[]")
    try:
        runs = json.loads(txt or "[]")
        return runs if isinstance(runs, list) else []
    except ValueError:
        return []


def save_runs(runs, prj=None):
    prj = prj or QgsProject.instance()
    prj.writeEntry(SCOPE, KEY, json.dumps(runs[-MAX_RUNS:], ensure_ascii=False))


def add_run(rec, prj=None):
    runs = load_runs(prj)
    runs.append(rec)
    save_runs(runs, prj)
    return len(runs) - 1


class _Recorder:
    def __init__(self):
        self.original = None
        self.depth = 0
        self.gui_open = {}     # history entry id -> (run index, start time) of toolbox runs in progress
        self.registry = None

    # ---- processing.run -----------------------------------------------------
    def install(self):
        if self.original is None:
            try:
                from processing.core.Processing import Processing
            except Exception:
                Processing = None
            if Processing is not None:
                self.original = Processing.runAlgorithm
                rec = self

                def runAlgorithm(algOrName, parameters, onFinish=None, feedback=None, context=None):  # noqa: N802
                    return rec.run(algOrName, parameters, onFinish, feedback, context)

                runAlgorithm._biruni_methodology = True
                Processing.runAlgorithm = staticmethod(runAlgorithm)
        if self.registry is None:
            try:
                from qgis.gui import QgsGui

                reg = QgsGui.historyProviderRegistry()
                reg.entryAdded.connect(self._on_entry_added)
                reg.entryUpdated.connect(self._on_entry_updated)
                self.registry = reg
            except Exception:
                self.registry = None

    def uninstall(self):
        if self.original is not None:
            try:
                from processing.core.Processing import Processing

                Processing.runAlgorithm = staticmethod(self.original)
            except Exception:
                pass
            self.original = None
        if self.registry is not None:
            for sig, slot in ((self.registry.entryAdded, self._on_entry_added),
                              (self.registry.entryUpdated, self._on_entry_updated)):
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            self.registry = None

    def run(self, algOrName, parameters, onFinish, feedback, context):  # noqa: N803
        now = time.monotonic()
        # a toolbox run that never reported back (dialog closed, crash) stops blocking after an hour
        self.gui_open = {k: v for k, v in self.gui_open.items() if now - v[1] < 3600}
        record = self.depth == 0 and not self.gui_open and _main_thread()
        alg = algOrName
        if record and not hasattr(alg, "id"):
            alg = QgsApplication.processingRegistry().algorithmById(str(algOrName))
        self.depth += 1
        try:
            results = self.original(algOrName, parameters, onFinish, feedback, context)
        except Exception:
            self.depth -= 1
            if record and alg is not None:
                self._safe_add(alg, parameters, {}, False, "script")
            raise
        self.depth -= 1
        if record and alg is not None:
            self._safe_add(alg, parameters, results, True, "script")
        return results

    @staticmethod
    def _safe_add(alg, parameters, results, ok, via, raw_map=None):
        try:
            if alg.id() in IGNORED_ALGS:
                return None
            return add_run(_record_from(alg, dict(parameters or {}), results, ok, via, raw_map))
        except Exception:
            return None

    # ---- toolbox dialogs (QGIS history provider) ---------------------------
    def _on_entry_added(self, entry_id, entry, *args):
        try:
            if entry.providerId != "processing":
                return
            data = entry.entry or {}
            alg = QgsApplication.processingRegistry().algorithmById(data.get("algorithm_id", ""))
            if alg is None:
                return
            inputs = (data.get("parameters") or {}).get("inputs", {}) or {}
            idx = self._safe_add(alg, inputs, {}, None, "dialog", raw_map=inputs)
            if idx is not None:
                self.gui_open[entry_id] = (idx, time.monotonic())
        except Exception:
            pass

    def _on_entry_updated(self, entry_id, entry, *args):
        idx = self.gui_open.pop(entry_id, (None, 0))[0]
        if idx is None:
            return
        try:
            data = entry if isinstance(entry, dict) else (getattr(entry, "entry", None) or {})
            results = data.get("results")
            if results is None:
                return
            runs = load_runs()
            if idx >= len(runs):
                return
            run = runs[idx]
            alg = QgsApplication.processingRegistry().algorithmById(run["alg"])
            run["ok"] = bool(results)
            if alg is not None and results:
                run["outputs"] = _record_from(alg, {}, results, True, "dialog", raw_map={})["outputs"]
            save_runs(runs)
        except Exception:
            pass


_RECORDER = _Recorder()


def install_recorder():
    """Start recording Processing runs (call once when the plugin starts)."""
    _RECORDER.install()


def uninstall_recorder():
    _RECORDER.uninstall()


# =============================================================================
# Facts: data sources, activity, layouts
# =============================================================================
ORIGINS = {
    # key: (detect patterns, en name, ar name, licence, reference)
    "osm": (("overpass", "openstreetmap", "gis_osm", "osm_", ".osm", "geofabrik", "tile.openstreetmap"),
            "OpenStreetMap", "خريطة الشارع المفتوحة (OpenStreetMap)", "ODbL 1.0",
            "OpenStreetMap contributors ({year}). OpenStreetMap [Data set]. OpenStreetMap Foundation. "
            "Available under the Open Database License (ODbL 1.0). https://www.openstreetmap.org"),
    "overture": (("overture",), "Overture Maps", "خرائط أوفرتشر (Overture Maps)", "CDLA-Permissive 2.0 / ODbL",
                 "Overture Maps Foundation ({year}). Overture Maps data [Data set]. https://overturemaps.org"),
    "copernicus_dem": (("copernicus_dsm", "copernicus-dem", "copernicus dem", "cop30", "cop-dem", "glo-30", "glo30",
                        "dem 30 m"),
                       "Copernicus DEM GLO-30", "نموذج الارتفاعات الرقمي كوبرنيكوس (Copernicus DEM GLO-30)",
                       "Copernicus DEM licence",
                       "European Space Agency & Airbus ({dem_year}). Copernicus DEM GLO-30 [Data set]. "
                       "https://doi.org/10.5270/ESA-c5d3d65"),
    "worldcover": (("worldcover", "land cover (10 m)"), "ESA WorldCover 10 m 2021",
                   "الغطاء الأرضي ESA WorldCover بدقة 10 م (2021)", "CC BY 4.0",
                   "Zanaga, D., Van De Kerchove, R., Daems, D., et al. (2022). ESA WorldCover 10 m 2021 v200 "
                   "[Data set]. Zenodo. https://doi.org/10.5281/zenodo.7254221"),
    "sentinel2": (("sentinel-2", "sentinel2", "sentinel_2", "s2a_", "s2b_", "s2c_", "earth-search"),
                  "Copernicus Sentinel-2", "مرئيات القمر الصناعي سنتينل-2 (Copernicus Sentinel-2)",
                  "Copernicus data licence",
                  "European Space Agency ({year}). Copernicus Sentinel-2 MSI Level-2A imagery [Data set]. "
                  "Contains modified Copernicus Sentinel data. Accessed via Element 84 Earth Search. "
                  "https://dataspace.copernicus.eu"),
    "geoboundaries": (("geoboundaries", "gbopen"), "geoBoundaries", "الحدود الإدارية geoBoundaries", "CC BY 4.0",
                      "Runfola, D., Anderson, A., Baier, H., et al. (2020). geoBoundaries: A global database of "
                      "political administrative boundaries. PLoS ONE, 15(4), e0231866. "
                      "https://doi.org/10.1371/journal.pone.0231866"),
    "srtm": (("srtm",), "SRTM", "نموذج الارتفاعات SRTM", "Public domain",
             "NASA JPL (2013). NASA Shuttle Radar Topography Mission Global 1 arc second [Data set]. "
             "https://doi.org/10.5067/MEaSUREs/SRTM/SRTMGL1.003"),
    "landsat": (("landsat", "lc08_", "lc09_", "le07_"), "Landsat", "مرئيات لاندسات (Landsat)", "Public domain",
                "U.S. Geological Survey ({year}). Landsat Collection 2 Level-2 Science Products [Data set]. "
                "https://doi.org/10.5066/P9OGBGM6"),
    "google": (("google.com", "googleapis", "mt0.google", "mt1.google"), "Google basemap", "خريطة أساس جوجل",
               "Google terms of service", "Google ({year}). Google Maps / Satellite basemap. https://maps.google.com"),
    "esri": (("arcgisonline", "server.arcgisonline", "esri"), "Esri basemap", "خريطة أساس إسري (Esri)",
             "Esri terms of use", "Esri ({year}). World Imagery basemap. https://www.arcgis.com"),
}
# outputs of these tools are data sources of that origin
ALG_ORIGINS = {"biruni:downloadosm": "osm", "biruni:downloadoverture": "overture", "biruni:downloaddem": "copernicus_dem",
               "biruni:downloadlandcover": "worldcover", "biruni:downloadsentinel2": "sentinel2",
               "biruni:downloadboundaries": "geoboundaries"}

SOFTWARE_REFS = [
    "QGIS Development Team ({year}). QGIS Geographic Information System (version {qgis}). Open Source Geospatial "
    "Foundation. https://qgis.org",
    "GDAL/OGR contributors ({year}). GDAL/OGR Geospatial Data Abstraction software Library. Open Source Geospatial "
    "Foundation. https://gdal.org",
    "Biruni GIS ({year}). Biruni GIS (version {ver}), a free desktop GIS built on QGIS. "
    "https://github.com/HatemTarekGIS/biruni-gis",
]
METHOD_REFS = {
    "horn": "Horn, B. K. P. (1981). Hill shading and the reflectance map. Proceedings of the IEEE, 69(1), 14-47. "
            "https://doi.org/10.1109/PROC.1981.11918",
    "shepard": "Shepard, D. (1968). A two-dimensional interpolation function for irregularly-spaced data. "
               "Proceedings of the 1968 ACM National Conference, 517-524. https://doi.org/10.1145/800186.810616",
    "silverman": "Silverman, B. W. (1986). Density Estimation for Statistics and Data Analysis. Chapman and Hall.",
    "rouse": "Rouse, J. W., Haas, R. H., Schell, J. A., & Deering, D. W. (1974). Monitoring vegetation systems in the "
             "Great Plains with ERTS. NASA Special Publication, 351, 309-317.",
    "dijkstra": "Dijkstra, E. W. (1959). A note on two problems in connexion with graphs. Numerische Mathematik, 1, "
                "269-271. https://doi.org/10.1007/BF01386390",
    "delaunay": "Delaunay, B. (1934). Sur la sphère vide. Bulletin de l'Académie des Sciences de l'URSS, 7, 793-800.",
    "clark_evans": "Clark, P. J., & Evans, F. C. (1954). Distance to nearest neighbor as a measure of spatial "
                   "relationships in populations. Ecology, 35(4), 445-453. https://doi.org/10.2307/1931034",
    "douglas": "Douglas, D. H., & Peucker, T. K. (1973). Algorithms for the reduction of the number of points required "
               "to represent a digitized line or its caricature. Cartographica, 10(2), 112-122. "
               "https://doi.org/10.3138/FM57-6770-U75U-7727",
}


def detect_origin(text):
    t = (text or "").lower()
    for key, spec in ORIGINS.items():
        if any(p in t for p in spec[0]):
            return key
    return ""


def _safe_source(lyr):
    """A source description without credentials: a file name or a web host."""
    src = lyr.source() or ""
    if lyr.providerType() in ("wms", "xyz", "arcgismapserver", "arcgisfeatureserver", "wfs", "vectortile"):
        m = re.search(r"https?://([^/?&'\"\s]+)", src.replace("%3A", ":").replace("%2F", "/"))
        return m.group(1) if m else lyr.providerType()
    if lyr.providerType() == "memory":
        return "memory"
    src = re.sub(r"(password|pwd|token|key)=\S+", r"\1=***", src, flags=re.I)
    path = src.split("|")[0]
    if os.path.exists(path) or "/" in path or "\\" in path:
        return os.path.basename(path)
    return src[:120]


def _geom_kind(lyr):
    try:
        t = lyr.geometryType()
    except Exception:
        return "table"
    return {Qgis.GeometryType.Point: "point", Qgis.GeometryType.Line: "line",
            Qgis.GeometryType.Polygon: "polygon"}.get(t, "table")


def _crs_text(crs):
    if crs is None or not crs.isValid():
        return ""
    desc = crs.description() or ""
    return f"{desc} ({crs.authid()})" if desc and crs.authid() else (crs.authid() or desc)


def _unit_name(unit, lang):
    try:
        s = QgsUnitTypes.toString(unit)
    except Exception:
        s = ""
    s = (s or "").lower()
    ar = {"meters": "متر", "kilometers": "كم", "degrees": "درجة", "feet": "قدم", "miles": "ميل",
          "centimeters": "سم", "millimeters": "مم", "yards": "ياردة", "nautical miles": "ميل بحري"}
    return ar.get(s, s) if lang == "ar" else s


def _layer_info(lyr):
    info = {"kind": "data", "layer_id": lyr.id(), "name": lyr.name(), "provider": lyr.providerType(),
            "source": _safe_source(lyr), "crs": _crs_text(lyr.crs()), "crs_authid": lyr.crs().authid(),
            "type": "", "count": None, "resolution": None, "units": None, "bands": None}
    md = lyr.metadata()
    meta = " ".join([md.title(), md.abstract(), " ".join(md.rights()), " ".join(md.licenses())])
    info["origin"] = detect_origin(" ".join([lyr.source(), lyr.name(), meta]))
    if isinstance(lyr, QgsVectorLayer):
        info["type"] = _geom_kind(lyr)
        try:
            info["count"] = int(lyr.featureCount())
        except Exception:
            pass
    elif isinstance(lyr, QgsRasterLayer):
        if lyr.providerType() in ("wms", "xyz"):
            info["type"] = "basemap"
        else:
            info["type"] = "raster"
            try:
                info["resolution"] = round(lyr.rasterUnitsPerPixelX(), 6)
                info["units"] = lyr.crs().mapUnits()
                info["bands"] = lyr.bandCount()
                info["size"] = f"{lyr.width()} x {lyr.height()}"
            except Exception:
                pass
    else:
        info["type"] = "other"
    return info


def _file_info(path):
    """Facts about an input file that is not a project layer."""
    p = path.split("|")[0]
    if not os.path.exists(p):
        return None
    lyr = QgsRasterLayer(p, os.path.splitext(os.path.basename(p))[0])
    if not lyr.isValid():
        lyr = QgsVectorLayer(path, os.path.splitext(os.path.basename(p))[0], "ogr")
    if not lyr.isValid():
        return None
    info = _layer_info(lyr)
    info["layer_id"] = ""
    return info


def _activity_events(prj):
    """Activity log events of the project (plugin logger when running, else the stored log)."""
    try:
        import qgis.utils

        plugin = qgis.utils.plugins.get("biruni")
        if plugin is not None and getattr(plugin, "activity", None) is not None and prj is QgsProject.instance():
            return plugin.activity.snapshot()[0]
    except Exception:
        pass
    try:
        from ..activity import ActivityStore, storage_path

        return ActivityStore(storage_path(prj)).read()[0]
    except Exception:
        return []


def _layouts(prj):
    out = []
    try:
        for lay in prj.layoutManager().printLayouts():
            pages = lay.pageCollection().pageCount()
            size = ""
            if pages:
                p = lay.pageCollection().page(0)
                ps = p.pageSize()
                size = f"{ps.width():g} x {ps.height():g} mm"
            maps = 0
            from qgis.core import QgsLayoutItemMap

            maps = sum(1 for i in lay.items() if isinstance(i, QgsLayoutItemMap))
            atlas = bool(lay.atlas() and lay.atlas().enabled())
            out.append({"name": lay.name(), "pages": pages, "size": size, "maps": maps, "atlas": atlas})
    except Exception:
        pass
    return out


def _alg_key(alg_id):
    base = alg_id.split(":", 1)[-1].lower()
    return ALIASES.get(alg_id, ALIASES.get(base, base))


def _important_params(alg, run):
    """{name: human value} of the parameters that matter (no outputs, no temporary names)."""
    return {k: v for k, v in run["params"].items() if v not in (None, "", [], {})}


# =============================================================================
# Steps
# =============================================================================
def methodology_steps(project=None):
    """Ordered, de-duplicated steps of the project's work.

    Each step is a dict with at least: id, kind (data / crs / tool / edit / layout),
    ts, title (English), include (True); tools add alg, params (machine values),
    inputs and outputs (layer names), count (number of identical runs).
    """
    prj = project or QgsProject.instance()
    reg = QgsApplication.processingRegistry()
    runs = [r for r in load_runs(prj) if r.get("alg") not in IGNORED_ALGS]
    events = _activity_events(prj)
    layers = prj.mapLayers()
    added_ts = {}
    for e in events:
        if e.get("category") == "layer" and e.get("action") == "added" and e.get("target_id"):
            added_ts.setdefault(e["target_id"], e.get("ts") or EARLY)

    # ---- tool runs: drop failures, merge identical runs ----------------------
    steps, seen = [], {}
    produced = {}   # key (layer id / path) -> (step, output name)
    used_names = {}  # key -> the name a layer had when a later tool used it
    for r in runs:
        used_names.update(r.get("names") or {})
    for r in runs:
        if r.get("ok") is False:
            continue
        sig = r["alg"] + json.dumps(r.get("params", {}), sort_keys=True, ensure_ascii=False)
        if sig in seen:
            st = seen[sig]
            st["count"] += 1
            for o in r.get("outputs", {}).values():
                for k in o.get("keys", []):
                    produced.setdefault(k, (st, st["outputs"][0] if st["outputs"] else "", o.get("name") or ""))
            continue
        alg = reg.algorithmById(r["alg"])
        inputs, lparams = [], {}
        for pname, names in r.get("layers", {}).items():
            keys = (r.get("layer_keys") or {}).get(pname, [])
            fixed = []
            for j, n in enumerate(names):
                k = keys[j] if j < len(keys) else ""
                if k in produced and (n or "").lower() in GENERIC_NAMES | {produced[k][2].lower()}:
                    n = produced[k][1]
                elif k in layers:
                    n = layers[k].name()
                if n:
                    fixed.append(n)
            lparams[pname] = fixed
            inputs += [n for n in fixed if n not in inputs]
        outs, out_names = [], []
        for o in r.get("outputs", {}).values():
            n = o.get("name") or ""
            if n.lower() in GENERIC_NAMES:
                n = o.get("desc") or n
            later = [used_names[k] for k in o.get("keys", []) if k in used_names]
            if later:
                n = later[0]
            for k in o.get("keys", []):
                lyr = layers.get(k)
                if lyr is None:
                    for cand in layers.values():
                        if _norm_path(cand.source()) == k:
                            lyr = cand
                            break
                if lyr is not None:
                    n = lyr.name()
                    break
            out_names.append((o, n))
            if n and n not in outs:
                outs.append(n)
        st = {"id": f"tool{len(steps)}", "kind": "tool", "ts": r.get("ts") or EARLY, "alg": r["alg"],
              "key": _alg_key(r["alg"]), "title": alg.displayName() if alg else r["alg"],
              "params": r.get("params", {}), "layer_params": lparams, "units": r.get("units", {}),
              "inputs": inputs, "outputs": outs, "count": 1, "include": True,
              "keys_in": r.get("keys_in", []), "origin": ALG_ORIGINS.get(r["alg"], "")}
        if st["key"] == "rastercalculator" and _is_ndvi(st["params"]):
            st["key"] = "ndvi"
        seen[sig] = st
        steps.append(st)
        for o, n in out_names:
            for k in o.get("keys", []):
                produced.setdefault(k, (st, n, o.get("name") or ""))

    # activity "tool run" events not covered by the recorder (older projects, recorder off)
    recorded_times = [(r["alg"], _parse(r.get("ts"))) for r in runs]
    for e in events:
        if e.get("category") != "tool" or e.get("action") != "run":
            continue
        aid, t = e.get("target_id") or "", _parse(e.get("ts"))
        if not aid or aid in IGNORED_ALGS:
            continue
        if any(a == aid and t and rt and abs((t - rt).total_seconds()) < 120 for a, rt in recorded_times):
            continue
        sig = "activity:" + aid
        if sig in seen:
            seen[sig]["count"] += 1
            continue
        alg = reg.algorithmById(aid)
        st = {"id": f"tool{len(steps)}", "kind": "tool", "ts": e.get("ts") or EARLY, "alg": aid,
              "key": _alg_key(aid), "title": alg.displayName() if alg else (e.get("target") or aid), "params": {},
              "inputs": [], "outputs": [], "count": 1, "include": True, "keys_in": [],
              "origin": ALG_ORIGINS.get(aid, "")}
        seen[sig] = st
        steps.append(st)

    # ---- data sources ----------------------------------------------------------
    data = []
    for lid, lyr in layers.items():
        src = _norm_path(lyr.source())
        made = produced.get(lid) or produced.get(src)
        info = _layer_info(lyr)
        if made is not None:
            if not made[0].get("origin"):
                continue  # a product of the analysis, not a data source
            info["origin"] = made[0]["origin"]
            info["ts"] = made[0]["ts"]
        else:
            info["ts"] = added_ts.get(lid, EARLY)
        data.append(info)
    known = {d["layer_id"] for d in data} | {_norm_path(lyr.source()) for lyr in layers.values()}
    for st in steps:
        for k in st.get("keys_in", []):
            if k in known or k in produced or k in layers or not (os.sep in k or "/" in k):
                continue
            known.add(k)
            info = _file_info(k)
            if info:
                info["ts"] = EARLY
                data.append(info)
    data.sort(key=lambda d: d["ts"] or EARLY)
    for i, d in enumerate(data):
        d.update({"id": f"data{i}", "title": d["name"], "include": True})

    # ---- coordinate system ----------------------------------------------------
    crs_steps = []
    pcrs = prj.crs()
    if pcrs.isValid():
        others = sorted({d["crs_authid"] for d in data if d.get("crs_authid") and d["crs_authid"] != pcrs.authid()
                         and d["type"] != "basemap"})
        crs_steps.append({"id": "crs", "kind": "crs", "ts": EARLY, "title": "Coordinate reference system",
                          "crs": _crs_text(pcrs), "authid": pcrs.authid(), "others": others, "include": True,
                          "units": pcrs.mapUnits()})

    # ---- manual edits (activity log) -------------------------------------------
    edits = {}
    for e in events:
        if e.get("category") != "edit" or e.get("action") not in ("features_added", "geometries_changed",
                                                                  "attributes_changed", "features_deleted",
                                                                  "fields_added"):
            continue
        key = e.get("target") or "?"
        row = edits.setdefault(key, {"id": f"edit{len(edits)}", "kind": "edit", "ts": e.get("ts") or EARLY,
                                     "title": key, "layer": key, "include": True, "features_added": 0,
                                     "geometries_changed": 0, "attributes_changed": 0, "features_deleted": 0,
                                     "fields_added": 0})
        row[e["action"]] += int(e.get("count") or 0)
    edit_steps = [s for s in edits.values() if any(s[a] for a in ("features_added", "geometries_changed",
                                                                  "attributes_changed", "features_deleted",
                                                                  "fields_added"))]

    # ---- map products ------------------------------------------------------------
    layout_steps = [{"id": f"layout{i}", "kind": "layout", "ts": LATE, "title": lay["name"], "include": True, **lay}
                    for i, lay in enumerate(_layouts(prj))]

    body = sorted(steps + edit_steps, key=lambda s: s["ts"] or EARLY)
    return data + crs_steps + body + layout_steps


def _parse(ts):
    try:
        return dt.datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


def _is_ndvi(params):
    expr = str(params.get("EXPRESSION") or params.get("FORMULA") or "")
    e = re.sub(r"\s+", "", expr)
    m = re.search(r"\(([^()]+)-([^()]+)\)/\(([^()]+)\+([^()]+)\)", e)
    return bool(m and {m.group(1), m.group(2)} == {m.group(3), m.group(4)})


# =============================================================================
# Sentence templates (content, not UI: written for academic prose)
# =============================================================================
ALIASES = {
    "warpreproject": "reprojectlayer", "reprojectraster": "reprojectlayer",
    "cliprasterbymasklayer": "cliprasterbymask", "cliprasterbyextent": "cliprasterbyextent",
    "clipvectorbyextent": "extractbyextent", "extractbyextent": "extractbyextent",
    "zonalstatistics": "zonalstatisticsfb", "fieldcalculator": "fieldcalculator",
    "advancedpythonfieldcalculator": "fieldcalculator", "randompointsinsidepolygons": "randompointsinpolygons",
    "multiringconstantbuffer": "multiringbuffer", "heatmapkerneldensityestimation": "kerneldensity",
    "voronoipolygons": "voronoipolygons", "mergevectorlayers": "mergevectorlayers", "merge": "mergeraster",
    "joinattributesbylocationsummary": "joinattributesbylocation", "selectbyattribute": "selectbyexpression",
    "extractbyattribute": "extractbyexpression", "rasterize": "rasterize", "fillnodata": "fillnodata",
    "dissolve": "dissolve", "buffer": "buffer", "polygonize": "polygonize", "rastersampling": "rastersampling",
    "assignprojection": "assignprojection", "defineprojection": "assignprojection",
    "downloadopendata": "opendata", "opendata": "opendata",
}

# key: (en sentence, ar sentence, en short label, ar short label, reference key or "")
# placeholders: parameter names ({INPUT}, {DISTANCE} ...), {in} = input layer(s), {out} = output(s)
T = {
    "reprojectlayer": (
        "The layer {in} was reprojected to the {TARGET_CRS} coordinate reference system.",
        "تم إسقاط طبقة {in} على نظام الإحداثيات {TARGET_CRS}.",
        "Reproject to {TARGET_CRS}", "إسقاط إلى {TARGET_CRS}", ""),
    "assignprojection": (
        "The coordinate reference system {CRS} was assigned to {in} without transforming its coordinates.",
        "تم تعيين نظام الإحداثيات {CRS} لطبقة {in} دون تحويل إحداثياتها.",
        "Assign CRS {CRS}", "تعيين نظام الإحداثيات {CRS}", ""),
    "clip": (
        "The features of {INPUT} were clipped to the boundary of {OVERLAY}, so that only the study area was retained.",
        "تم قص عناصر طبقة {INPUT} بحدود طبقة {OVERLAY} للاقتصار على منطقة الدراسة.",
        "Clip by {OVERLAY}", "قص بحدود {OVERLAY}", ""),
    "cliprasterbymask": (
        "The raster {INPUT} was clipped to the extent of the mask layer {MASK}.",
        "تم قص الطبقة الشبكية {INPUT} بحدود طبقة القناع {MASK}.",
        "Clip raster by {MASK}", "قص الطبقة الشبكية بحدود {MASK}", ""),
    "cliprasterbyextent": (
        "The raster {INPUT} was clipped to a rectangular extent covering the study area.",
        "تم قص الطبقة الشبكية {INPUT} بمستطيل يغطي منطقة الدراسة.",
        "Clip raster by extent", "قص الطبقة الشبكية بنطاق", ""),
    "extractbyextent": (
        "The features of {INPUT} falling within the study area extent were extracted.",
        "تم استخلاص عناصر طبقة {INPUT} الواقعة داخل نطاق منطقة الدراسة.",
        "Extract by extent", "استخلاص بالنطاق", ""),
    "buffer": (
        "A buffer zone of {DISTANCE} was generated around the features of {INPUT}{dissolved}.",
        "تم إنشاء نطاق تأثير (Buffer) بمسافة {DISTANCE} حول عناصر طبقة {INPUT}{dissolved}.",
        "Buffer {DISTANCE}", "نطاق تأثير {DISTANCE}", ""),
    "multiringbuffer": (
        "{RINGS} concentric buffer rings, {DISTANCE} apart, were generated around the features of {INPUT}.",
        "تم إنشاء {RINGS} من حلقات النطاق المتتالية بفاصل {DISTANCE} حول عناصر طبقة {INPUT}.",
        "Multi-ring buffer", "نطاقات متعددة الحلقات", ""),
    "dissolve": (
        "The features of {INPUT} were dissolved{by_field} into unified geometries.",
        "تم دمج عناصر طبقة {INPUT}{by_field} في أشكال هندسية موحدة (Dissolve).",
        "Dissolve", "دمج العناصر", ""),
    "intersection": (
        "The intersection of {INPUT} and {OVERLAY} was computed, keeping only the areas common to both layers "
        "together with their attributes.",
        "تم حساب التقاطع بين طبقتي {INPUT} و{OVERLAY} مع الإبقاء على المناطق المشتركة وخصائص الطبقتين.",
        "Intersection with {OVERLAY}", "تقاطع مع {OVERLAY}", ""),
    "union": (
        "A union overlay of {INPUT} and {OVERLAY} was performed.",
        "تم إجراء عملية اتحاد (Union) بين طبقتي {INPUT} و{OVERLAY}.",
        "Union", "اتحاد الطبقات", ""),
    "difference": (
        "The parts of {INPUT} overlapping {OVERLAY} were removed (difference overlay).",
        "تم حذف أجزاء طبقة {INPUT} المتداخلة مع طبقة {OVERLAY} (عملية الفرق).",
        "Difference with {OVERLAY}", "الفرق مع {OVERLAY}", ""),
    "symmetricaldifference": (
        "The symmetrical difference between {INPUT} and {OVERLAY} was computed.",
        "تم حساب الفرق المتماثل بين طبقتي {INPUT} و{OVERLAY}.",
        "Symmetrical difference", "الفرق المتماثل", ""),
    "slope": (
        "The slope of the terrain was derived from the elevation model {INPUT}{zfactor}.",
        "تم اشتقاق درجات الانحدار من نموذج الارتفاعات الرقمي {INPUT}{zfactor}.",
        "Slope", "حساب الانحدار", "horn"),
    "aspect": (
        "The aspect (slope direction) was derived from the elevation model {INPUT}.",
        "تم اشتقاق اتجاه الانحدار من نموذج الارتفاعات الرقمي {INPUT}.",
        "Aspect", "اتجاه الانحدار", "horn"),
    "hillshade": (
        "A hillshade of {INPUT} was produced with the light source at azimuth {AZIMUTH}° and altitude {V_ANGLE}°.",
        "تم إنتاج طبقة تظليل للتضاريس من {INPUT} بزاوية سمت للإضاءة {AZIMUTH}° وارتفاع {V_ANGLE}°.",
        "Hillshade", "تظليل التضاريس", "horn"),
    "ruggednessindex": (
        "The terrain ruggedness index was computed from {INPUT}.",
        "تم حساب مؤشر وعورة التضاريس من {INPUT}.",
        "Ruggedness index", "مؤشر الوعورة", ""),
    "contour": (
        "Contour lines with an interval of {INTERVAL} were generated from {INPUT}.",
        "تم توليد خطوط الكنتور بفاصل رأسي {INTERVAL} من {INPUT}.",
        "Contours every {INTERVAL}", "خطوط كنتور كل {INTERVAL}", ""),
    "zonalstatisticsfb": (
        "Zonal statistics ({STATISTICS}) of the raster {INPUT_RASTER} were calculated for each zone of {INPUT}.",
        "تم حساب الإحصاءات النطاقية ({STATISTICS}) لقيم الطبقة الشبكية {INPUT_RASTER} داخل كل نطاق من نطاقات "
        "طبقة {INPUT}.",
        "Zonal statistics", "إحصاءات نطاقية", ""),
    "zonalhistogram": (
        "The area of each class of {INPUT_RASTER} was counted within every zone of {INPUT_VECTOR} (zonal histogram).",
        "تم حساب عدد خلايا كل فئة من فئات {INPUT_RASTER} داخل كل نطاق من نطاقات {INPUT_VECTOR}.",
        "Zonal histogram", "المدرج التكراري النطاقي", ""),
    "rastercalculator": (
        "A new raster was computed with the raster calculator using the expression {expr}.",
        "تم إنتاج طبقة شبكية جديدة بواسطة حاسبة الطبقات الشبكية باستخدام المعادلة {expr}.",
        "Raster calculator", "حاسبة الطبقات الشبكية", ""),
    "ndvi": (
        "The Normalized Difference Vegetation Index (NDVI) was calculated as the normalized difference of the "
        "near-infrared and red bands, {expr}.",
        "تم حساب مؤشر الغطاء النباتي (NDVI) بوصفه الفرق المعياري بين نطاقي الأشعة تحت الحمراء القريبة والأحمر، "
        "{expr}.",
        "NDVI", "مؤشر NDVI", "rouse"),
    "reclassifybytable": (
        "The raster {INPUT_RASTER} was reclassified into {classes} classes according to a reclassification table.",
        "تم إعادة تصنيف الطبقة الشبكية {INPUT_RASTER} إلى {classes} فئات وفق جدول لإعادة التصنيف.",
        "Reclassify", "إعادة التصنيف", ""),
    "reclassifybylayer": (
        "The raster {INPUT_RASTER} was reclassified using the class ranges stored in {INPUT_TABLE}.",
        "تم إعادة تصنيف الطبقة الشبكية {INPUT_RASTER} وفق نطاقات الفئات المخزنة في {INPUT_TABLE}.",
        "Reclassify", "إعادة التصنيف", ""),
    "idwinterpolation": (
        "A continuous surface was interpolated from {in} using Inverse Distance Weighting (IDW){power}, with a cell "
        "size of {PIXEL_SIZE}.",
        "تم استيفاء سطح متصل من {in} بطريقة مقلوب المسافة الموزونة (IDW){power} بحجم خلية {PIXEL_SIZE}.",
        "IDW interpolation", "استيفاء IDW", "shepard"),
    "tininterpolation": (
        "A continuous surface was interpolated from {in} using a Triangulated Irregular Network (TIN), with a cell "
        "size of {PIXEL_SIZE}.",
        "تم استيفاء سطح متصل من {in} بطريقة شبكة المثلثات غير المنتظمة (TIN) بحجم خلية {PIXEL_SIZE}.",
        "TIN interpolation", "استيفاء TIN", "delaunay"),
    "kerneldensity": (
        "A kernel density surface of {INPUT} was estimated with a search radius of {RADIUS} and a cell size of "
        "{PIXEL_SIZE}.",
        "تم تقدير كثافة النواة (Kernel Density) لعناصر طبقة {INPUT} بنصف قطر بحث {RADIUS} وحجم خلية {PIXEL_SIZE}.",
        "Kernel density", "كثافة النواة", "silverman"),
    "linedensity": (
        "Line density of {INPUT} was computed with a search radius of {RADIUS}.",
        "تم حساب كثافة الخطوط لطبقة {INPUT} بنصف قطر بحث {RADIUS}.",
        "Line density", "كثافة الخطوط", ""),
    "joinattributesbylocation": (
        "The attributes of {JOIN} were joined to {INPUT} according to their spatial relationship ({PREDICATE}).",
        "تم ربط خصائص طبقة {JOIN} بطبقة {INPUT} وفق العلاقة المكانية بينهما ({PREDICATE}).",
        "Spatial join", "ربط مكاني", ""),
    "joinattributestable": (
        "The attributes of {INPUT_2} were joined to {INPUT} through the common field {FIELD} = {FIELD_2}.",
        "تم ربط خصائص {INPUT_2} بطبقة {INPUT} من خلال الحقل المشترك {FIELD} = {FIELD_2}.",
        "Join by field", "ربط بالحقول", ""),
    "fieldcalculator": (
        "A field named {FIELD_NAME} was calculated for {INPUT} using the expression {FORMULA}.",
        "تم حساب الحقل {FIELD_NAME} في طبقة {INPUT} باستخدام المعادلة {FORMULA}.",
        "Calculate {FIELD_NAME}", "حساب الحقل {FIELD_NAME}", ""),
    "selectbyexpression": (
        "The features of {INPUT} satisfying the condition {EXPRESSION} were selected.",
        "تم تحديد عناصر طبقة {INPUT} التي تحقق الشرط {EXPRESSION}.",
        "Select by expression", "تحديد بالشرط", ""),
    "extractbyexpression": (
        "The features of {INPUT} satisfying the condition {EXPRESSION} were extracted into a new layer.",
        "تم استخلاص عناصر طبقة {INPUT} التي تحقق الشرط {EXPRESSION} في طبقة جديدة.",
        "Extract by expression", "استخلاص بالشرط", ""),
    "selectbylocation": (
        "The features of {INPUT} that {PREDICATE} {INTERSECT} were selected.",
        "تم تحديد عناصر طبقة {INPUT} وفق علاقتها المكانية ({PREDICATE}) بطبقة {INTERSECT}.",
        "Select by location", "تحديد مكاني", ""),
    "extractbylocation": (
        "The features of {INPUT} that {PREDICATE} {INTERSECT} were extracted.",
        "تم استخلاص عناصر طبقة {INPUT} وفق علاقتها المكانية ({PREDICATE}) بطبقة {INTERSECT}.",
        "Extract by location", "استخلاص مكاني", ""),
    "centroids": (
        "The centroids of the features of {INPUT} were generated.",
        "تم توليد نقاط المراكز الهندسية لعناصر طبقة {INPUT}.",
        "Centroids", "المراكز الهندسية", ""),
    "fixgeometries": (
        "Invalid geometries in {INPUT} were repaired.",
        "تم إصلاح الأشكال الهندسية غير الصالحة في طبقة {INPUT}.",
        "Fix geometries", "إصلاح الأشكال الهندسية", ""),
    "mergevectorlayers": (
        "The layers {LAYERS} were merged into a single layer.",
        "تم دمج الطبقات {LAYERS} في طبقة واحدة.",
        "Merge layers", "دمج الطبقات", ""),
    "mergeraster": (
        "The rasters {INPUT} were mosaicked into a single raster.",
        "تم دمج الطبقات الشبكية {INPUT} في فسيفساء واحدة.",
        "Mosaic rasters", "دمج الطبقات الشبكية", ""),
    "countpointsinpolygon": (
        "The number of points of {POINTS} falling within each polygon of {POLYGONS} was counted.",
        "تم حساب عدد نقاط طبقة {POINTS} الواقعة داخل كل مضلع من مضلعات طبقة {POLYGONS}.",
        "Count points in polygons", "عدّ النقاط في المضلعات", ""),
    "voronoipolygons": (
        "Voronoi (Thiessen) polygons were generated from the points of {INPUT}.",
        "تم توليد مضلعات فورونوي (ثيسن) من نقاط طبقة {INPUT}.",
        "Voronoi polygons", "مضلعات ثيسن", ""),
    "delaunaytriangulation": (
        "A Delaunay triangulation of the points of {INPUT} was constructed.",
        "تم إنشاء شبكة مثلثات ديلوني لنقاط طبقة {INPUT}.",
        "Delaunay triangulation", "مثلثات ديلوني", "delaunay"),
    "polygonize": (
        "The raster {INPUT} was converted to polygons.",
        "تم تحويل الطبقة الشبكية {INPUT} إلى مضلعات.",
        "Raster to polygons", "تحويل إلى مضلعات", ""),
    "rasterize": (
        "The vector layer {INPUT} was converted to a raster.",
        "تم تحويل الطبقة الخطية {INPUT} إلى طبقة شبكية.",
        "Rasterize", "تحويل إلى شبكية", ""),
    "fillnodata": (
        "Gaps (no-data cells) in {INPUT} were filled by interpolation from the surrounding cells.",
        "تم ملء الفجوات (الخلايا عديمة القيمة) في {INPUT} بالاستيفاء من الخلايا المجاورة.",
        "Fill no-data", "ملء الفجوات", ""),
    "rastersampling": (
        "The values of {RASTERCOPY} were sampled at the locations of {INPUT}.",
        "تم استخراج قيم {RASTERCOPY} عند مواقع نقاط طبقة {INPUT}.",
        "Sample raster values", "استخراج قيم الخلايا", ""),
    "randompointsinpolygons": (
        "Random sample points were generated within the polygons of {INPUT}.",
        "تم توليد نقاط عينة عشوائية داخل مضلعات طبقة {INPUT}.",
        "Random points", "نقاط عشوائية", ""),
    "shortestpathpointtopoint": (
        "The shortest path between two points was computed on the network {INPUT}.",
        "تم حساب أقصر مسار بين نقطتين على شبكة {INPUT}.",
        "Shortest path", "أقصر مسار", "dijkstra"),
    "serviceareafrompoint": (
        "The service area reachable within {TRAVEL_COST2} from a start point was determined on the network {INPUT}.",
        "تم تحديد منطقة الخدمة التي يمكن بلوغها ضمن {TRAVEL_COST2} من نقطة البداية على شبكة {INPUT}.",
        "Service area", "منطقة الخدمة", "dijkstra"),
    "nearestneighbouranalysis": (
        "A nearest neighbour analysis of {INPUT} was carried out to test the spatial distribution pattern of the "
        "points.",
        "تم إجراء تحليل الجار الأقرب لطبقة {INPUT} لاختبار نمط التوزيع المكاني للنقاط.",
        "Nearest neighbour analysis", "تحليل الجار الأقرب", "clark_evans"),
    "distancematrix": (
        "A distance matrix between {INPUT} and {TARGET} was computed.",
        "تم حساب مصفوفة المسافات بين {INPUT} و{TARGET}.",
        "Distance matrix", "مصفوفة المسافات", ""),
    "simplifygeometries": (
        "The geometries of {INPUT} were simplified with a tolerance of {TOLERANCE}.",
        "تم تبسيط الأشكال الهندسية لطبقة {INPUT} بقيمة تسامح {TOLERANCE}.",
        "Simplify", "تبسيط", "douglas"),
    "statisticsbycategories": (
        "Statistics of {VALUES_FIELD_NAME} were summarised for each category of {CATEGORIES_FIELD_NAME} in {INPUT}.",
        "تم تلخيص إحصاءات الحقل {VALUES_FIELD_NAME} لكل فئة من فئات {CATEGORIES_FIELD_NAME} في طبقة {INPUT}.",
        "Statistics by category", "إحصاءات حسب الفئة", ""),
    "rasterlayerstatistics": (
        "Descriptive statistics of {INPUT} were calculated.",
        "تم حساب الإحصاءات الوصفية للطبقة الشبكية {INPUT}.",
        "Raster statistics", "إحصاءات الطبقة الشبكية", ""),
    "calculatevectoroverlaps": (
        "The area of overlap between {INPUT} and {LAYERS} was calculated for each feature.",
        "تم حساب مساحة التداخل بين {INPUT} و{LAYERS} لكل عنصر.",
        "Overlap analysis", "تحليل التداخل", ""),
    "sumlinelengths": (
        "The total length of the lines of {LINES} within each polygon of {POLYGONS} was calculated.",
        "تم حساب مجموع أطوال خطوط {LINES} داخل كل مضلع من مضلعات {POLYGONS}.",
        "Sum line lengths", "مجموع أطوال الخطوط", ""),
    "savefeatures": (
        "The layer {INPUT} was saved to a new file.",
        "تم حفظ طبقة {INPUT} في ملف جديد.",
        "Save to file", "حفظ في ملف", ""),
    "createpointslayerfromtable": (
        "A point layer was created from the coordinates stored in {INPUT}.",
        "تم إنشاء طبقة نقطية من الإحداثيات المخزنة في {INPUT}.",
        "Points from table", "نقاط من جدول", ""),
    # ---- Biruni open data ------------------------------------------------------
    "downloadosm": (
        "OpenStreetMap data ({DATA}) were downloaded for the study area through the Overpass API.",
        "تم تنزيل بيانات خريطة الشارع المفتوحة (OpenStreetMap) ({DATA}) لمنطقة الدراسة عبر واجهة Overpass.",
        "Download OpenStreetMap", "تنزيل OpenStreetMap", ""),
    "downloadoverture": (
        "Overture Maps data were downloaded for the study area.",
        "تم تنزيل بيانات Overture Maps لمنطقة الدراسة.",
        "Download Overture Maps", "تنزيل Overture Maps", ""),
    "downloaddem": (
        "The Copernicus DEM GLO-30 elevation model (30 m) was downloaded for the study area.",
        "تم تنزيل نموذج الارتفاعات الرقمي Copernicus DEM GLO-30 بدقة 30 م لمنطقة الدراسة.",
        "Download Copernicus DEM", "تنزيل نموذج الارتفاعات", ""),
    "downloadlandcover": (
        "Land cover data from ESA WorldCover (10 m, 2021) were obtained for the study area.",
        "تم الحصول على بيانات الغطاء الأرضي ESA WorldCover بدقة 10 م (2021) لمنطقة الدراسة.",
        "Download WorldCover", "تنزيل الغطاء الأرضي", ""),
    "downloadsentinel2": (
        "The least-cloudy Sentinel-2 scene of the period ({PRODUCT}, maximum cloud cover {CLOUD}%) was obtained for "
        "the study area at a pixel size of {RESOLUTION} m.",
        "تم الحصول على أقل مرئيات سنتينل-2 غيومًا خلال الفترة ({PRODUCT}، بحد أقصى للغيوم {CLOUD}%) لمنطقة الدراسة "
        "بحجم بكسل {RESOLUTION} م.",
        "Download Sentinel-2", "تنزيل مرئية سنتينل-2", ""),
    "downloadboundaries": (
        "Administrative boundaries ({LEVEL}) of {COUNTRY} were obtained from geoBoundaries.",
        "تم الحصول على الحدود الإدارية ({LEVEL}) لـ{COUNTRY} من قاعدة بيانات geoBoundaries.",
        "Download boundaries", "تنزيل الحدود الإدارية", ""),
    "opendata": (
        "Open data for the study area were downloaded with Biruni GIS' open data tool.",
        "تم تنزيل البيانات المفتوحة لمنطقة الدراسة بأداة البيانات المفتوحة في برنامج Biruni GIS.",
        "Download open data", "تنزيل بيانات مفتوحة", ""),
}

GENERIC = ("The tool “{title}” was applied to {in}{settings}.", "تم تطبيق أداة «{title}» على {in}{settings}.")
GENERIC_NO_INPUT = ("The tool “{title}” was used{settings}.", "تم استخدام أداة «{title}»{settings}.")

L = {  # fixed phrases of the chapter
    "title": ("Methodology", "منهجية الدراسة"),
    "intro": ("This chapter describes the data and the procedures used in the study “{project}”. All spatial data "
              "were prepared and analysed in Biruni GIS {ver}, a free desktop geographic information system built on "
              "QGIS {qgis}, following the sequence of steps summarised in Figure 1.",
              "يتناول هذا الفصل البيانات والإجراءات المستخدمة في دراسة «{project}». وقد تم إعداد البيانات المكانية "
              "وتحليلها جميعًا باستخدام برنامج Biruni GIS (الإصدار {ver})، وهو نظام معلومات جغرافية مكتبي مجاني "
              "مبني على QGIS {qgis}، وفق تسلسل الخطوات الموضح في الشكل (1)."),
    "data_intro": ("The study relied on {n} datasets.", "اعتمدت الدراسة على {n} من مجموعات البيانات."),
    "data_intro1": ("The study relied on one dataset.", "اعتمدت الدراسة على مجموعة بيانات واحدة."),
    "data_vec": ("{name} is a {gtype} layer of {count} feature(s){origin}.",
                 "وتمثل {name} طبقة {gtype} يبلغ عدد عناصرها {count}{origin}."),
    "data_ras": ("{name} is a raster with a cell size of {res}{origin}.",
                 "وتمثل {name} طبقة شبكية بحجم خلية {res}{origin}."),
    "data_map": ("{name} was used as a background basemap{origin}.",
                 "واستُخدمت {name} خريطةَ أساس للخلفية{origin}."),
    "data_other": ("{name} was also used{origin}.", "كما استُخدمت {name}{origin}."),
    "origin": (", obtained from {src} ({lic})", "، وقد تم الحصول عليها من {src} ({lic})"),
    "origin_nolic": (", obtained from {src}", "، وقد تم الحصول عليها من {src}"),
    "file": (" (file {src})", " (ملف {src})"),
    "crs": ("All layers were managed in the {crs} coordinate reference system{units}.",
            "تم توحيد جميع الطبقات على نظام الإحداثيات {crs}{units}."),
    "crs_units": (", whose map units are {u}", "، ووحدة قياسه {u}"),
    "crs_others": (" Layers stored in other systems ({others}) were transformed to it on the fly.",
                   " كما تم تحويل الطبقات المخزنة بأنظمة أخرى ({others}) إليه آليًا أثناء العرض."),
    "proc_intro": ("The analysis comprised the following steps.", "وقد اشتمل التحليل على الخطوات الآتية."),
    "times": (" (run {n} times with the same settings)", " (عدد مرات التنفيذ بالإعدادات نفسها: {n})"),
    "into": (", producing {out}", "، ونتجت عن ذلك طبقة {out}"),
    "edit_intro": ("The data were also edited manually: ", "كما تم تحرير البيانات يدويًا: "),
    "edit_layer": ("in {layer}, {parts}", "ففي طبقة {layer} {parts}"),
    "features_added": ("{n} features were added", "تمت إضافة {n} من العناصر"),
    "geometries_changed": ("{n} geometries were reshaped", "تم تعديل شكل {n} من العناصر"),
    "attributes_changed": ("{n} attribute values were edited", "تم تعديل {n} من القيم الوصفية"),
    "features_deleted": ("{n} features were deleted", "تم حذف {n} من العناصر"),
    "fields_added": ("{n} fields were added", "تمت إضافة {n} من الحقول"),
    "layouts": ("The final results were presented in the following map layouts: {items}.",
                "وقد تم إخراج النتائج النهائية في لوحات الخرائط الآتية: {items}."),
    "layouts1": ("The final results were presented in the map layout {items}.",
                 "وقد تم إخراج النتائج النهائية في لوحة الخرائط {items}."),
    "layout_item": ("“{name}” ({size}{atlas})", "«{name}» ({size}{atlas})"),
    "atlas": (", atlas", "، أطلس"),
    "credit": ("The data sources and software used are acknowledged in Table 1 and in the references.",
               "ويوضح الجدول (1) وقائمة المراجع مصادر البيانات والبرمجيات المستخدمة مع الإشارة إلى تراخيصها."),
    "fig": ("Figure 1. Workflow of the study.", "شكل (1): مخطط سير العمل في الدراسة."),
    "table": ("Table 1. Data sources.", "جدول (1): مصادر البيانات."),
    "refs": ("References", "المراجع"),
    "steps": ("Processing steps", "خطوات المعالجة"),
    "cols": (("Dataset", "Type", "Source", "Coordinate system", "Detail", "Licence"),
             ("البيانات", "النوع", "المصدر", "نظام الإحداثيات", "التفاصيل", "الترخيص")),
    "inputs": ("Input data", "البيانات المدخلة"),
    "outputs": ("Results", "النتائج"),
    "first": ("First, ", "في البداية، "),
    "then": ("Then, ", "ثم "),
    "next": ("Next, ", "بعد ذلك، "),
    "after": ("After that, ", "وبعد ذلك "),
    "last": ("Finally, ", "وأخيرًا، "),
    "and": (" and ", " و"),
    "yes": ("yes", "نعم"), "no": ("no", "لا"),
    "dissolved": (", and the overlapping buffers were dissolved", "، مع دمج النطاقات المتداخلة"),
    "by_field": (" by {f}", " حسب الحقل {f}"),
    "zfactor": (" with a vertical exaggeration (Z factor) of {z}", " بمعامل مبالغة رأسية {z}"),
    "power": (" with a distance power of {p}", " بأس مسافة {p}"),
    "settings": (" with the following settings: {s}", " بالإعدادات الآتية: {s}"),
    "point": ("point", "نقطية"), "line": ("line", "خطية"), "polygon": ("polygon", "مضلعات"),
    "table_t": ("table", "جدولية"), "raster": ("raster", "شبكية"), "basemap": ("basemap", "خريطة أساس"),
    "other": ("layer", "طبقة"), "features": ("{n} features", "{n} عنصر"), "cells": ("cell {r}", "خلية {r}"),
    "local": ("Local data", "بيانات محلية"), "unnamed": ("Untitled project", "مشروع بدون عنوان"),
    "the_area": ("the study area", "منطقة الدراسة"),
}


def _lang(lang):
    return "ar" if str(lang or "").lower().startswith("ar") else "en"


def _p(key, lang):
    v = L[key]
    return v[1] if _lang(lang) == "ar" else v[0]


def _q(name, lang):
    return f"«{name}»" if _lang(lang) == "ar" else f"“{name}”"


def _join(items, lang):
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if _lang(lang) == "ar":
        return "، ".join(items[:-1]) + " و" + items[-1]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return f"{f:.6g}"


def _crs_label(v):
    if isinstance(v, dict):
        v = v.get("authid") or v.get("wkt") or ""
    crs = QgsCoordinateReferenceSystem(str(v)) if v else QgsCoordinateReferenceSystem()
    return _crs_text(crs) if crs.isValid() else str(v)


class _Values(dict):
    def __missing__(self, key):
        return "…"


def _param_values(step, lang):
    """Human values of the step's parameters, keyed by parameter name."""
    reg = QgsApplication.processingRegistry()
    alg = reg.algorithmById(step.get("alg", ""))
    vals = _Values()
    params = step.get("params", {})
    layer_names = {}
    if alg is None:
        for k, v in params.items():
            vals[k] = _short(v, 80) if not isinstance(v, str) else v
        return vals
    prj = QgsProject.instance()
    for p in alg.parameterDefinitions():
        name = p.name()
        if name not in params:
            continue
        v = params[name]
        t = _ptype(p)
        if t in LAYER_PARAM_TYPES:
            vs = v if isinstance(v, list) else [v]
            names = list(step.get("layer_params", {}).get(name) or [])
            for one in ([] if names else vs):
                n, _k = _layer_ref(one, prj)
                if n:
                    names.append(n)
            layer_names[name] = names
            vals[name] = _join([_q(n, lang) for n in names], lang) or _p("the_area", lang)
        elif t == "enum":
            opts = p.options()
            idx = v if isinstance(v, list) else [v]
            labels = []
            for i in idx:
                try:
                    labels.append(opts[int(i)])
                except (TypeError, ValueError, IndexError):
                    labels.append(str(i))
            vals[name] = _join(labels, lang)
        elif t == "boolean":
            vals[name] = _p("yes", lang) if v in (True, "true", "True", 1) else _p("no", lang)
        elif t == "crs":
            vals[name] = _crs_label(v)
        elif t == "distance" and step.get("units", {}).get(name):
            u, ok = QgsUnitTypes.decodeDistanceUnit(step["units"][name])
            vals[name] = f"{_num(v)} {_unit_name(u, lang) if ok else ''}".strip()
        elif t in ("distance", "duration"):
            unit = ""
            parent = p.parentParameterName() if hasattr(p, "parentParameterName") else ""
            src = params.get(parent) if parent else None
            if src is not None:
                n, keys = _layer_ref(src, prj)
                lyr = None
                for k in keys:
                    lyr = prj.mapLayer(k)
                    if lyr is None:
                        for cand in prj.mapLayers().values():
                            if _norm_path(cand.source()) == k:
                                lyr = cand
                                break
                    if lyr is not None:
                        break
                if lyr is None and keys and os.path.exists(str(keys[-1]).split("|")[0]):
                    info = _file_info(keys[-1])
                    if info and info.get("crs_authid"):
                        unit = _unit_name(QgsCoordinateReferenceSystem(info["crs_authid"]).mapUnits(), lang)
                elif lyr is not None:
                    unit = _unit_name(lyr.crs().mapUnits(), lang)
            if t == "duration":
                unit = ""
            vals[name] = f"{_num(v)} {unit}".strip()
        elif t == "number":
            vals[name] = _num(v)
        elif t == "field":
            vals[name] = _join([_q(x, lang) for x in (v if isinstance(v, list) else [v]) if x], lang)
        elif t in ("expression", "string"):
            vals[name] = _q(str(v), lang) if v not in (None, "") else "…"
        elif t == "extent":
            vals[name] = _p("the_area", lang)
        else:
            vals[name] = _short(v, 80) if not isinstance(v, str) else v
    # convenience placeholders
    vals["in"] = _join([_q(n, lang) for n in step.get("inputs", [])], lang) or _p("the_area", lang)
    vals["out"] = _join([_q(n, lang) for n in step.get("outputs", [])], lang)
    expr = params.get("EXPRESSION") or params.get("FORMULA") or ""
    vals["expr"] = _q(str(expr), lang) if expr else "…"
    vals["dissolved"] = _p("dissolved", lang) if params.get("DISSOLVE") in (True, "true", 1) else ""
    fields = params.get("FIELD") or []
    fields = fields if isinstance(fields, list) else [fields]
    vals["by_field"] = _p("by_field", lang).format(f=_join([_q(f, lang) for f in fields if f], lang)) \
        if any(fields) and step.get("key") == "dissolve" else ""
    z = params.get("Z_FACTOR")
    vals["zfactor"] = _p("zfactor", lang).format(z=_num(z)) if z not in (None, "", 1, 1.0, "1") else ""
    pw = params.get("DISTANCE_COEFFICIENT")
    vals["power"] = _p("power", lang).format(p=_num(pw)) if pw not in (None, "") else ""
    table = params.get("TABLE")
    vals["classes"] = str(len(table) // 3) if isinstance(table, list) and table else "…"
    if step.get("key") in ("idwinterpolation", "tininterpolation"):
        data = str(params.get("INTERPOLATION_DATA") or "")
        src = data.split("::~::")[0] if data else ""
        if src:
            n, _k = _layer_ref(src, prj)
            vals["in"] = _q(n, lang) if n else vals["in"]
    return vals


def _settings_text(step, lang, vals):
    """Non-default, non-advanced parameter values for the generic sentence."""
    reg = QgsApplication.processingRegistry()
    alg = reg.algorithmById(step.get("alg", ""))
    if alg is None:
        return ""
    parts = []
    for p in alg.parameterDefinitions():
        name = p.name()
        if name not in step.get("params", {}) or _ptype(p) in LAYER_PARAM_TYPES or p.isDestination():
            continue
        if p.flags() & (Qgis.ProcessingParameterFlag.Advanced | Qgis.ProcessingParameterFlag.Hidden):
            continue
        v = step["params"][name]
        if v in (None, "", []) or str(v) == str(p.defaultValue()):
            continue
        parts.append(f"{p.description()}: {vals.get(name)}")
        if len(parts) >= 4:
            break
    return _p("settings", lang).format(s="; ".join(parts) if lang != "ar" else "؛ ".join(parts)) if parts else ""


def step_sentence(step, lang="ar"):
    """One sentence describing a step (without the sequence connector)."""
    lang = _lang(lang)
    k = step.get("kind")
    i = 1 if lang == "ar" else 0
    if k == "tool":
        vals = _param_values(step, lang)
        tpl = T.get(step.get("key"))
        if tpl and step.get("params"):
            s = tpl[i].format_map(vals)
        elif tpl and "{" not in tpl[i].replace("{in}", ""):
            s = tpl[i].format_map(vals)
        else:
            g = GENERIC if step.get("inputs") else GENERIC_NO_INPUT
            s = g[i].format(title=step.get("label") or step.get("title"), **{"in": vals["in"]},
                            settings=_settings_text(step, lang, vals))
        if step.get("outputs") and step.get("key") not in ("savefeatures", "selectbyexpression", "selectbylocation"):
            s = s.rstrip(".") + _p("into", lang).format(out=vals["out"]) + "."
        if step.get("count", 1) > 1:
            s = s.rstrip(".") + _p("times", lang).format(n=step["count"]) + "."
        return s
    if k == "edit":
        parts = [_p(a, lang).format(n=step[a]) for a in ("features_added", "geometries_changed", "attributes_changed",
                                                         "features_deleted", "fields_added") if step.get(a)]
        return _p("edit_layer", lang).format(layer=_q(step["layer"], lang), parts=_join(parts, lang)) + "."
    if k == "crs":
        units = _p("crs_units", lang).format(u=_unit_name(step["units"], lang)) if step.get("units") is not None \
            else ""
        s = _p("crs", lang).format(crs=step["crs"], units=units)
        if step.get("others"):
            s += _p("crs_others", lang).format(others=_join(step["others"], lang))
        return s
    if k == "data":
        return _data_sentence(step, lang)
    if k == "layout":
        return _p("layout_item", lang).format(name=step["name"], size=step.get("size", ""),
                                              atlas=_p("atlas", lang) if step.get("atlas") else "")
    return step.get("title", "")


def _origin_name(key, lang):
    o = ORIGINS.get(key)
    return (o[2] if _lang(lang) == "ar" else o[1]) if o else ""


def _data_sentence(d, lang):
    origin = ""
    if d.get("origin") in ORIGINS:
        o = ORIGINS[d["origin"]]
        origin = _p("origin", lang).format(src=_origin_name(d["origin"], lang), lic=o[3])
    elif d.get("source") and d["source"] != "memory":
        origin = _p("file", lang).format(src=d["source"])
    name = _q(d.get("label") or d["name"], lang)
    if d["type"] in ("point", "line", "polygon", "table"):
        gt = _p("table_t" if d["type"] == "table" else d["type"], lang)
        s = _p("data_vec", lang).format(name=name, gtype=gt, count=d.get("count") if d.get("count") is not None
                                        else "…", origin=origin)
        return s.replace("feature(s)", "feature" if d.get("count") == 1 else "features")
    if d["type"] == "raster":
        return _p("data_ras", lang).format(name=name, res=_res_text(d, lang), origin=origin)
    if d["type"] == "basemap":
        return _p("data_map", lang).format(name=name, origin=origin)
    return _p("data_other", lang).format(name=name, origin=origin)


def _res_text(d, lang):
    if d.get("resolution") is None:
        return "…"
    return f"{_num(d['resolution'])} {_unit_name(d.get('units'), lang)}".strip()


def step_label(step, lang="ar"):
    """A short label for lists and the diagram."""
    if step.get("label"):
        return step["label"]
    lang = _lang(lang)
    i = 3 if lang == "ar" else 2
    if step.get("kind") == "tool":
        tpl = T.get(step.get("key"))
        if tpl:
            vals = _param_values(step, lang)
            return tpl[i].format_map(vals).replace("«", "").replace("»", "").replace("“", "").replace("”", "")
        return step.get("title") or step.get("alg")
    if step.get("kind") == "edit":
        return ("تحرير يدوي: " if lang == "ar" else "Manual editing: ") + step["layer"]
    if step.get("kind") == "crs":
        return ("نظام الإحداثيات: " if lang == "ar" else "CRS: ") + (step.get("authid") or step.get("crs", ""))
    if step.get("kind") == "layout":
        return ("لوحة خريطة: " if lang == "ar" else "Map layout: ") + step["name"]
    if step.get("kind") == "data":
        o = ORIGINS.get(step.get("origin"))
        return f"{step['name']} - {o[1]}" if o else step["name"]
    return step.get("title", "")


# =============================================================================
# The chapter
# =============================================================================
def _project_name(prj):
    return prj.title() or os.path.splitext(os.path.basename(prj.fileName()))[0]


def _versions():
    try:
        from ..version import VERSION
    except Exception:
        VERSION = ""
    return VERSION, Qgis.version().split("-")[0]


def methodology_document(lang="ar", steps=None, project=None):
    """Everything needed to write the chapter: title, paragraphs, step list, sources table, references."""
    lang = _lang(lang)
    prj = project or QgsProject.instance()
    steps = methodology_steps(prj) if steps is None else steps
    steps = [s for s in steps if s.get("include", True)]
    ver, qgis = _versions()
    i = 1 if lang == "ar" else 0
    paras = [L["intro"][i].format(project=_project_name(prj) or _p("unnamed", lang), ver=ver, qgis=qgis)]

    data = [s for s in steps if s["kind"] == "data"]
    if data:
        intro = _p("data_intro1", lang) if len(data) == 1 else _p("data_intro", lang).format(n=len(data))
        paras.append(" ".join([intro] + [_data_sentence(d, lang) for d in data]))
    crs = [s for s in steps if s["kind"] == "crs"]
    if crs:
        paras.append(step_sentence(crs[0], lang))

    work = [s for s in steps if s["kind"] in ("tool", "edit")]
    tools = [s for s in work if s["kind"] == "tool"]
    sentences = []
    n, idx = len(tools), 0
    for s in work:
        sent = step_sentence(s, lang)
        if s["kind"] == "edit":
            sent = _p("edit_intro", lang) + sent
        else:
            if n > 1:
                conn = "first" if idx == 0 else "last" if idx == n - 1 else ("then", "next", "after")[idx % 3]
                sent = _connect(_p(conn, lang), sent, lang)
            idx += 1
        sentences.append(sent)
    if sentences:
        chunk = []
        for j, sent in enumerate(sentences):
            chunk.append(sent)
            if len(chunk) == 4 or j == len(sentences) - 1:
                prefix = [_p("proc_intro", lang)] if not paras or j < 4 else []
                paras.append(" ".join(prefix + chunk))
                chunk = []

    layouts = [s for s in steps if s["kind"] == "layout"]
    if layouts:
        paras.append(_p("layouts1" if len(layouts) == 1 else "layouts", lang).format(
            items=_join([step_sentence(x, lang) for x in layouts], lang)))
    paras.append(_p("credit", lang))

    # data sources table
    rows = []
    for d in data:
        o = ORIGINS.get(d.get("origin"))
        tkey = {"table": "table_t"}.get(d["type"], d["type"] or "other")
        detail = (_p("features", lang).format(n=d["count"]) if d.get("count") is not None else
                  _p("cells", lang).format(r=_res_text(d, lang)) if d["type"] == "raster" else "")
        rows.append([d.get("label") or d["name"], _p(tkey, lang) if tkey in L else d["type"],
                     _origin_name(d.get("origin"), lang) or (d.get("source") or _p("local", lang)),
                     d.get("crs_authid") or d.get("crs") or "", detail, o[3] if o else ""])

    # references
    year = dt.date.today().year
    refs = set()
    for d in data:
        o = ORIGINS.get(d.get("origin"))
        if o:
            refs.add(o[4].format(year=year, dem_year=2022))
    for s in tools:
        if s.get("origin") in ORIGINS:
            refs.add(ORIGINS[s["origin"]][4].format(year=year, dem_year=2022))
        tpl = T.get(s.get("key"))
        if tpl and tpl[4]:
            refs.add(METHOD_REFS[tpl[4]])
    for r in SOFTWARE_REFS:
        refs.add(r.format(year=year, qgis=qgis, ver=ver))
    refs = sorted(refs, key=lambda r: r.lower())

    step_list = [step_label(s, lang) for s in work + layouts]
    return {"lang": lang, "title": _p("title", lang), "paragraphs": paras, "steps": step_list,
            "sources_header": list(L["cols"][i]), "sources": rows, "references": refs,
            "figure_caption": _p("fig", lang), "table_caption": _p("table", lang),
            "refs_title": _p("refs", lang), "steps_title": _p("steps", lang), "rtl": lang == "ar",
            "step_dicts": steps}


def _connect(conn, sent, lang):
    if lang == "ar":
        return conn + sent
    first = sent.split(" ", 1)[0]
    if first and first[0].isupper() and (len(first) == 1 or first[1:].islower()) and first not in PROPER:
        sent = sent[0].lower() + sent[1:]
    return conn + sent


PROPER = {"Voronoi", "Overture", "Delaunay", "Copernicus", "OpenStreetMap", "QGIS", "NDVI", "IDW", "TIN"}


def methodology_text(lang="ar", steps=None, project=None):
    """(title, [paragraphs]) of the methodology chapter."""
    doc = methodology_document(lang, steps, project)
    return doc["title"], doc["paragraphs"]


def plain_text(doc):
    """The whole chapter as plain text (for the clipboard and the .md file without markup)."""
    out = [doc["title"], ""]
    out += [p + "\n" for p in doc["paragraphs"]]
    out += [doc["steps_title"], ""] + [f"{i}. {s}" for i, s in enumerate(doc["steps"], 1)] + [""]
    out += [doc["table_caption"], "\t".join(doc["sources_header"])]
    out += ["\t".join(str(c) for c in r) for r in doc["sources"]] + [""]
    out += [doc["refs_title"], ""] + doc["references"]
    return "\n".join(out)


# =============================================================================
# Workflow diagram
# =============================================================================
def _diagram_model(steps, lang):
    steps = [s for s in steps if s.get("include", True)]
    inputs = [s.get("label") or (s["name"] + ("\n" + ORIGINS[s["origin"]][1] if s.get("origin") in ORIGINS else ""))
              for s in steps if s["kind"] == "data"]
    body = [s for s in steps if s["kind"] in ("crs", "tool", "edit")]
    procs = [step_label(s, lang) for s in body]
    consumed = set()
    for s in body:
        consumed.update(s.get("inputs", []))
    outs = []
    for s in body:
        for o in s.get("outputs", []):
            if o not in consumed and o not in outs:
                outs.append(o)
    outs = outs[-6:] + [step_label(s, lang) for s in steps if s["kind"] == "layout"]
    return inputs, procs, outs


def _font_family():
    try:
        from ..survey.report_pdf import font_family

        return font_family()
    except Exception:
        return "Tahoma"


def _render(painter, width, lang, model, measure_only=False):
    """Lay out and (unless measure_only) draw the flowchart. Returns the height used."""
    from qgis.PyQt.QtCore import QPointF, QRectF, Qt
    from qgis.PyQt.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainterPath, QPen, QPolygonF

    inputs, procs, outs = model
    rtl = _lang(lang) == "ar"
    margin, gap, row_gap = 24.0, 18.0, 46.0
    font = QFont(_font_family())
    font.setPixelSize(15)
    bold = QFont(font)
    bold.setBold(True)
    head = QFont(font)
    head.setPixelSize(13)
    head.setBold(True)
    fm = QFontMetricsF(font)
    flags = int(Qt.AlignmentFlag.AlignCenter.value | Qt.TextFlag.TextWordWrap.value)
    if painter is not None:
        painter.setLayoutDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)

    # Arabic labels often start with a Latin layer name: a right-to-left mark makes every line read right to left
    mark = "‏" if rtl else ""

    def text_h(t, w):
        return fm.boundingRect(QRectF(0, 0, w, 10000), flags, mark + t.replace("\n", "\n" + mark)).height()

    colors = {"in": ("#e6f2f1", "#0e7c74"), "proc": ("#ffffff", "#334155"), "out": ("#fdf2e3", "#b45309")}
    y = margin
    title = {"in": _p("inputs", lang), "out": _p("outputs", lang)}
    rows = []

    def grid(items, kind):
        nonlocal y
        if not items:
            return None
        per = min(len(items), 3 if width < 900 else 4)
        bw = (width - 2 * margin - (per - 1) * gap) / per
        top = y + 22
        boxes, row_h = [], 0
        for idx, t in enumerate(items):
            c = idx % per
            if c == 0 and idx:
                top += row_h + gap
                row_h = 0
            n_in_row = min(per, len(items) - (idx - c))
            off = (width - 2 * margin - (n_in_row * bw + (n_in_row - 1) * gap)) / 2
            col = (per - 1 - c) if rtl else c
            col = (n_in_row - 1 - c) if rtl else c
            x = margin + off + col * (bw + gap)
            h = max(44.0, text_h(t, bw - 16) + 18)
            boxes.append([QRectF(x, top, bw, h), t])
            row_h = max(row_h, h)
        # equalise heights per row
        for b in boxes:
            same = [o for o in boxes if abs(o[0].top() - b[0].top()) < 0.5]
            hh = max(o[0].height() for o in same)
            b[0].setHeight(hh)
        bottom = max(b[0].bottom() for b in boxes)
        frame = QRectF(margin - 8, y, width - 2 * margin + 16, bottom - y + 10)
        rows.append((kind, frame, boxes))
        y = bottom + 10 + row_gap
        return frame

    grid(inputs, "in")
    pw = min(width - 2 * margin, 560.0)
    for t in procs:
        h = max(44.0, text_h(t, pw - 20) + 18)
        rows.append(("proc", None, [[QRectF((width - pw) / 2, y, pw, h), t]]))
        y += h + row_gap
    grid(outs, "out")
    height = y - row_gap + margin
    if measure_only or painter is None:
        return height

    painter.setRenderHint(painter.RenderHint.Antialiasing, True)
    painter.setRenderHint(painter.RenderHint.TextAntialiasing, True)
    painter.fillRect(QRectF(0, 0, width, height), QColor("#ffffff"))

    def arrow(p1, p2):
        pen = QPen(QColor("#475569"), 2)
        painter.setPen(pen)
        painter.drawLine(p1, QPointF(p2.x(), p2.y() - 8))
        painter.setBrush(QBrush(QColor("#475569")))
        painter.drawPolygon(QPolygonF([QPointF(p2.x(), p2.y()), QPointF(p2.x() - 6, p2.y() - 10),
                                       QPointF(p2.x() + 6, p2.y() - 10)]))

    prev = None
    for kind, frame, boxes in rows:
        if frame is not None:
            painter.setPen(QPen(QColor(colors[kind][1]), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(frame, 10, 10)
            painter.setFont(head)
            painter.setPen(QColor(colors[kind][1]))
            painter.drawText(QRectF(frame.left() + 10, frame.top() + 2, frame.width() - 20, 20),
                             int((Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft).value
                                 | Qt.AlignmentFlag.AlignAbsolute.value | Qt.AlignmentFlag.AlignVCenter.value),
                             title[kind])
        top_c = QPointF(frame.center().x() if frame is not None else boxes[0][0].center().x(),
                        frame.top() if frame is not None else boxes[0][0].top())
        if prev is not None:
            arrow(prev, top_c)
        for rect, t in boxes:
            fill, line = colors[kind]
            painter.setPen(QPen(QColor(line), 1.6))
            painter.setBrush(QBrush(QColor(fill)))
            if kind == "proc":
                painter.drawRoundedRect(rect, 6, 6)
            else:
                path = QPainterPath()
                path.addRoundedRect(rect, 16, 16)
                painter.drawPath(path)
            painter.setPen(QColor("#0f172a"))
            painter.setFont(bold if kind == "proc" else font)
            painter.drawText(rect.adjusted(8, 4, -8, -4), flags, mark + t.replace("\n", "\n" + mark))
        bottom = frame.bottom() if frame is not None else boxes[0][0].bottom()
        prev = QPointF(top_c.x(), bottom)
    return height


def workflow_image(path, lang="ar", steps=None, width=1000):
    """Draw the workflow flowchart to a .png (2x resolution) or .svg file; returns the path."""
    from qgis.PyQt.QtCore import QRectF, QSize
    from qgis.PyQt.QtGui import QImage, QPainter

    steps = methodology_steps() if steps is None else steps
    model = _diagram_model(steps, lang)
    h = _render(None, float(width), lang, model, measure_only=True)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if path.lower().endswith(".svg"):
        from qgis.PyQt.QtSvg import QSvgGenerator

        gen = QSvgGenerator()
        gen.setFileName(path)
        gen.setSize(QSize(int(width), int(h)))
        gen.setViewBox(QRectF(0, 0, width, h))
        gen.setTitle(_p("fig", lang))
        p = QPainter(gen)
        _render(p, float(width), lang, model)
        p.end()
        return path
    scale = 2
    img = QImage(int(width * scale), int(h * scale), QImage.Format.Format_ARGB32)
    img.setDotsPerMeterX(int(96 * scale / 0.0254))
    img.setDotsPerMeterY(int(96 * scale / 0.0254))
    p = QPainter(img)
    p.scale(scale, scale)
    _render(p, float(width), lang, model)
    p.end()
    img.save(path, "PNG")
    return path


# =============================================================================
# Export
# =============================================================================
def docx_available():
    try:
        from ..vendor import ensure_vendor

        ensure_vendor()
    except Exception:
        pass
    try:
        import docx  # noqa: F401

        return True
    except Exception:
        return False


def export_methodology(path, lang="ar", steps=None, project=None):
    """Write the chapter next to `path` (extension decides; .docx falls back to .html + .md when python-docx is
    missing). Returns the list of written files (the main document first)."""
    doc = methodology_document(lang, steps, project)
    base, ext = os.path.splitext(path)
    ext = ext.lower()
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    png = workflow_image(base + " - workflow.png", lang, doc["step_dicts"])
    svg = workflow_image(base + " - workflow.svg", lang, doc["step_dicts"])
    written = []
    if ext == ".docx" and docx_available():
        written.append(_write_docx(base + ".docx", doc, png))
    elif ext == ".md":
        written.append(_write_md(base + ".md", doc, png))
        written.append(_write_html(base + ".html", doc, png))
    else:
        written.append(_write_html(base + ".html", doc, png))
        written.append(_write_md(base + ".md", doc, png))
    return written + [png, svg]


def _write_html(path, doc, png):
    rtl = doc["rtl"]
    e = html.escape
    img = os.path.basename(png)
    parts = [f"<!DOCTYPE html><html lang=\"{doc['lang']}\" dir=\"{'rtl' if rtl else 'ltr'}\"><head>"
             "<meta charset=\"utf-8\">"
             f"<title>{e(doc['title'])}</title><style>"
             "body{font-family:" + ("'Traditional Arabic','Simplified Arabic',Tahoma" if rtl else
                                    "'Times New Roman',Georgia,serif") + ";font-size:14pt;line-height:1.6;"
             "max-width:17cm;margin:2cm auto;text-align:justify}"
             "h1{font-size:20pt}h2{font-size:15pt;margin-top:1.4em}"
             "figure{text-align:center;margin:1.2em 0}figure img{max-width:100%;width:16cm}"
             "figcaption,caption{font-size:12pt;font-style:italic;padding:.3em}"
             "table{border-collapse:collapse;width:100%;font-size:11pt}"
             "th,td{border:1px solid #888;padding:4px 6px;vertical-align:top}th{background:#eee}"
             "ol.refs{padding-" + ("right" if rtl else "left") + ":1.2em}ol.refs li{direction:ltr;text-align:left;"
             "font-size:12pt}</style></head><body>",
             f"<h1>{e(doc['title'])}</h1>"]
    paras = doc["paragraphs"]
    parts.append(f"<p>{e(paras[0])}</p>")
    parts.append(f"<figure><img src=\"{e(img)}\" alt=\"{e(doc['figure_caption'])}\">"
                 f"<figcaption>{e(doc['figure_caption'])}</figcaption></figure>")
    parts += [f"<p>{e(p)}</p>" for p in paras[1:]]
    parts.append(f"<h2>{e(doc['steps_title'])}</h2><ol>")
    parts += [f"<li>{e(s)}</li>" for s in doc["steps"]]
    parts.append("</ol>")
    if doc["sources"]:
        parts.append(f"<table><caption>{e(doc['table_caption'])}</caption><tr>")
        parts += [f"<th>{e(h)}</th>" for h in doc["sources_header"]]
        parts.append("</tr>")
        for r in doc["sources"]:
            parts.append("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>")
        parts.append("</table>")
    parts.append(f"<h2>{e(doc['refs_title'])}</h2><ol class=\"refs\">")
    parts += [f"<li>{e(r)}</li>" for r in doc["references"]]
    parts.append("</ol></body></html>")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(parts))
    return path


def _md_cell(c):
    return str(c).replace("|", "\\|").replace("\n", " ")


def _write_md(path, doc, png):
    out = [f"# {doc['title']}", "", doc["paragraphs"][0], "",
           f"![{doc['figure_caption']}]({os.path.basename(png).replace(' ', '%20')})", "",
           f"*{doc['figure_caption']}*", ""]
    for p in doc["paragraphs"][1:]:
        out += [p, ""]
    out += [f"## {doc['steps_title']}", ""] + [f"{i}. {s}" for i, s in enumerate(doc["steps"], 1)] + [""]
    if doc["sources"]:
        out += [f"*{doc['table_caption']}*", "", "| " + " | ".join(doc["sources_header"]) + " |",
                "|" + "---|" * len(doc["sources_header"])]
        out += ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in doc["sources"]]
        out.append("")
    out += [f"## {doc['refs_title']}", ""] + [f"{i}. {r}" for i, r in enumerate(doc["references"], 1)]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    return path


def _write_docx(path, doc, png):
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm

    d = docx.Document()
    rtl = doc["rtl"]

    def para(text, style=None, align=None):
        p = d.add_paragraph(text, style=style)
        if rtl:
            pPr = p._p.get_or_add_pPr()
            from docx.oxml import OxmlElement

            bidi = OxmlElement("w:bidi")
            pPr.append(bidi)
            for r in p.runs:
                r.font.rtl = True
        if align is not None:
            p.alignment = align
        return p

    d.add_heading(doc["title"], level=1)
    para(doc["paragraphs"][0])
    d.add_picture(png, width=Cm(16))
    d.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    para(doc["figure_caption"], align=WD_ALIGN_PARAGRAPH.CENTER)
    for p in doc["paragraphs"][1:]:
        para(p)
    d.add_heading(doc["steps_title"], level=2)
    for s in doc["steps"]:
        para(s, style="List Number")
    if doc["sources"]:
        para(doc["table_caption"], align=WD_ALIGN_PARAGRAPH.CENTER)
        t = d.add_table(rows=1, cols=len(doc["sources_header"]))
        t.style = "Table Grid"
        for c, h in zip(t.rows[0].cells, doc["sources_header"]):
            c.text = h
        for r in doc["sources"]:
            cells = t.add_row().cells
            for c, v in zip(cells, r):
                c.text = str(v)
    d.add_heading(doc["refs_title"], level=2)
    for r in doc["references"]:
        d.add_paragraph(r, style="List Number")
    d.save(path)
    return path
