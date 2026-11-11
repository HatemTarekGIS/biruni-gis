"""Offline maps: a basemap kept in one file, and web maps that need no server of their own.

* A basemap pack is a PMTiles archive (for example an area cut out of a daily OpenStreetMap build of
  Protomaps: only that area's tiles are downloaded, once). The desktop map draws it as vector tiles through an
  MBTiles copy made beside it the first time (the engine reads MBTiles natively), styled with the bundled
  Protomaps style whose labels are in Arabic (``name:ar``, else the local name).
* Layers of the project can be made into a pack too (GDAL writes the vector tiles).
* A published site (share/pages_publish.py) carries MapLibre GL JS, pmtiles.js and an Arabic font with it:
  nothing is fetched from a CDN, so the site works on a network with no internet. Arabic labels are shaped by
  MapLibre itself (version 6 shapes Arabic and orders right-to-left text without a plugin) and drawn with the
  bundled Noto Sans Arabic through the style's ``font-faces``, so no glyph server is needed.
* PMTiles are read in pieces (HTTP range requests), so a site must be opened over http, not by double-clicking
  index.html: ``serve`` is a small preview server on this computer that answers range requests.
"""
import copy
import functools
import json
import os
import re
import shutil
import tempfile
import threading
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from . import pmtiles as pm

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "web")
STYLE_FILE = os.path.join(WEB_DIR, "basemap", "protomaps-light-ar.json")
FONT_FILE = os.path.join(WEB_DIR, "fonts", "NotoSansArabic-Regular.ttf")
FONT_NAME = "Biruni Sans"                  # the fontstack the web styles use; the file covers Arabic
DESKTOP_FONT = "Noto Sans Arabic Regular"
ARABIC_RANGES = ["U+0600-06FF", "U+0750-077F", "U+0870-08FF", "U+FB50-FDFF", "U+FE70-FEFF"]
OSM_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
BUILDS_URL = "https://build-metadata.protomaps.dev/builds.json"
BUILD_BASE = "https://build.protomaps.com/"
PROP = "biruni/basemap_pmtiles"
NAME_FIELD = ["coalesce", ["get", "name:ar"], ["get", "name"]]

# (file in resources/web, path in a published site)
ASSETS = (
    ("maplibre/maplibre-gl.mjs", "lib/maplibre-gl.mjs"),
    ("maplibre/maplibre-gl-worker.mjs", "lib/maplibre-gl-worker.mjs"),
    ("maplibre/maplibre-gl.css", "lib/maplibre-gl.css"),
    ("maplibre/LICENSE.txt", "lib/LICENSE-maplibre.txt"),
    ("pmtiles/pmtiles.js", "lib/pmtiles.js"),
    ("pmtiles/LICENSE.txt", "lib/LICENSE-pmtiles.txt"),
    ("fonts/NotoSansArabic-Regular.ttf", "lib/fonts/NotoSansArabic-Regular.ttf"),
    ("fonts/OFL.txt", "lib/fonts/OFL.txt"),
)


class OfflineMapError(Exception):
    pass


# ---- the web side ------------------------------------------------------------------------------------------
def copy_web_assets(out_dir, basemap=False):
    """The libraries and the font of a site, into out_dir/lib. Returns the relative paths written."""
    done = []
    items = list(ASSETS) + ([("basemap/LICENSE.txt", "lib/LICENSE-basemap-style.txt")] if basemap else [])
    for src, rel in items:
        dst = os.path.join(out_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(WEB_DIR, *src.split("/")), dst)
        done.append(rel)
    return done


def font_faces(base="lib/fonts/"):
    """The style's ``font-faces``: the bundled Arabic font for the Arabic blocks (other letters fall back to the
    browser's own fonts)."""
    return {FONT_NAME: [{"url": base + "NotoSansArabic-Regular.ttf", "unicode-range": ARABIC_RANGES}]}


def _load_style():
    with open(STYLE_FILE, encoding="utf-8") as f:
        return json.load(f)


def basemap_style(kind="web", source_url=None):
    """The bundled Protomaps style, made ready for the web page ("web") or the desktop map ("desktop")."""
    st = copy.deepcopy(_load_style())
    st.pop("sprite", None)
    st.pop("glyphs", None)
    if source_url:
        st["sources"]["protomaps"]["url"] = source_url
    layers = []
    for lyr in st["layers"]:
        lay = lyr.get("layout") or {}
        if "icon-image" in lay and "text-field" not in lay:
            continue                          # shields and one-way arrows: they need a sprite sheet
        for k in [k for k in lay if k.startswith("icon-")]:
            lay.pop(k)                        # there is no sprite sheet: icons are left out, labels stay
        for k in [k for k in (lyr.get("paint") or {}) if k.startswith("icon-")]:
            lyr["paint"].pop(k)
        if "text-field" in lay and '"name' in json.dumps(lay["text-field"]):
            lay["text-field"] = NAME_FIELD    # the Arabic name, else the local one: one rule both maps read
        if "text-font" in lay:
            lay["text-font"] = [FONT_NAME] if kind == "web" else [DESKTOP_FONT]
        layers.append(lyr)
    st["layers"] = layers
    if kind == "web":
        st["font-faces"] = font_faces()
    return st


def web_basemap_layers():
    """The layers of the basemap for a site page, drawing source "basemap"."""
    out = []
    for lyr in basemap_style("web")["layers"]:
        lyr = dict(lyr)
        if lyr.get("source") == "protomaps":
            lyr["source"] = "basemap"
        out.append(lyr)
    return out


# ---- the desktop side --------------------------------------------------------------------------------------
_font_done = False


def register_font():
    """The bundled Arabic font, known to the desktop map's labels."""
    global _font_done
    if _font_done:
        return
    from qgis.PyQt.QtGui import QFontDatabase

    QFontDatabase.addApplicationFont(FONT_FILE)
    _font_done = True


def mbtiles_for(path):
    """The MBTiles copy of a PMTiles file (made, or made again when the PMTiles file is newer)."""
    mbt = os.path.splitext(path)[0] + ".mbtiles"
    folder = os.path.dirname(path)
    if not os.access(folder or ".", os.W_OK):
        from qgis.core import QgsApplication

        folder = os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni", "basemaps")
        os.makedirs(folder, exist_ok=True)
        mbt = os.path.join(folder, os.path.splitext(os.path.basename(path))[0] + ".mbtiles")
    if not os.path.exists(mbt) or os.path.getmtime(mbt) < os.path.getmtime(path):
        pm.to_mbtiles(path, mbt)
    return mbt


def style_layer(layer, style=None):
    """Draw a vector tile layer with a MapLibre style (the bundled basemap style by default).
    Returns the converter's warnings."""
    from qgis.core import QgsMapBoxGlStyleConversionContext, QgsMapBoxGlStyleConverter

    register_font()
    conv = QgsMapBoxGlStyleConverter()
    ctx = QgsMapBoxGlStyleConversionContext()
    conv.convert(style or basemap_style("desktop"), ctx)
    if conv.renderer() is not None:
        layer.setRenderer(conv.renderer())
    labeling = conv.labeling()
    if labeling is not None:
        _straight_line_labels(labeling)
        layer.setLabeling(labeling)
        layer.setLabelsEnabled(True)
    return list(ctx.warnings())


def _straight_line_labels(labeling):
    """Labels along lines written straight, not letter by letter along the curve: the engine draws curved
    Arabic labels with their letters apart and in the wrong order, a straight one is shaped as a whole."""
    from qgis.core import Qgis

    styles = labeling.styles()
    for st in styles:
        pal = st.labelSettings()
        if pal.placement == Qgis.LabelPlacement.Curved:
            pal.placement = Qgis.LabelPlacement.Line
            st.setLabelSettings(pal)
    labeling.setStyles(styles)


def add_basemap(path, project=None, name=None):
    """A PMTiles basemap pack on the map, under the other layers. Returns the layer."""
    from qgis.core import QgsProject, QgsVectorTileLayer

    project = project or QgsProject.instance()
    head = pm.info(path)
    if head["tile_type"] != 1:
        raise OfflineMapError("raster")
    mbt = mbtiles_for(path)
    layer = QgsVectorTileLayer(f"type=mbtiles&url={mbt}", name or os.path.splitext(os.path.basename(path))[0])
    if not layer.isValid():
        raise OfflineMapError("invalid")
    style_layer(layer)
    layer.setCustomProperty(PROP, path)
    layer.setAttribution("© OpenStreetMap contributors")
    project.addMapLayer(layer, False)
    root = project.layerTreeRoot()
    root.insertLayer(len(root.children()), layer)
    return layer


def basemap_packs(project=None):
    """The PMTiles files of the basemap packs on the map."""
    from qgis.core import QgsProject

    project = project or QgsProject.instance()
    out = []
    for lyr in project.mapLayers().values():
        p = lyr.customProperty(PROP, "")
        if p and os.path.exists(p):
            out.append(p)
    return out


def _safe(name, used):
    base = re.sub(r"[^0-9A-Za-z_؀-ۿ]+", "_", name).strip("_")[:40] or "layer"
    cand, k = base, 2
    while cand.lower() in used:
        cand, k = f"{base}_{k}", k + 1
    used.add(cand.lower())
    return cand


def make_pack(layers, out, min_zoom=0, max_zoom=14, simplify=True, project=None):
    """Vector layers of the project in one PMTiles file, one tile layer each. Returns {layer name: tile layer}."""
    from osgeo import gdal
    from qgis.core import QgsProject, QgsVectorFileWriter, QgsVectorLayer

    project = project or QgsProject.instance()
    layers = [l for l in layers if isinstance(l, QgsVectorLayer) and l.isValid() and l.isSpatial()]
    if not layers:
        raise OfflineMapError("no layers")
    if not 0 <= min_zoom <= max_zoom <= 22:
        raise OfflineMapError("zoom")
    tmp = tempfile.mkdtemp(prefix="biruni_pack_")
    try:
        gpkg = os.path.join(tmp, "layers.gpkg")
        names, used = {}, set()
        for lyr in layers:
            n = _safe(lyr.name(), used)
            o = QgsVectorFileWriter.SaveVectorOptions()
            o.driverName = "GPKG"
            o.layerName = n
            o.fileEncoding = "UTF-8"
            o.actionOnExistingFile = (QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                                      if os.path.exists(gpkg) else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile)
            err = QgsVectorFileWriter.writeAsVectorFormatV3(lyr, gpkg, project.transformContext(), o)
            if err[0] != QgsVectorFileWriter.WriterError.NoError:
                raise OfflineMapError(err[1])
            names[lyr.name()] = n
        if os.path.exists(out):
            os.remove(out)
        opts = [f"MINZOOM={min_zoom}", f"MAXZOOM={max_zoom}", "NAME=" + os.path.splitext(os.path.basename(out))[0]]
        if simplify:
            opts += ["SIMPLIFICATION=1", f"SIMPLIFICATION_MAX_ZOOM={max(min_zoom, max_zoom - 1)}"]
        ds = gdal.VectorTranslate(out, gpkg, options=gdal.VectorTranslateOptions(format="PMTiles",
                                                                                datasetCreationOptions=opts))
        if ds is None:
            raise OfflineMapError(gdal.GetLastErrorMsg() or "PMTiles")
        ds = None  # noqa: F841 - flush to the disk
        return names
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- the Protomaps builds -----------------------------------------------------------------------------------
def latest_build(fetch=None, timeout=20):
    """The address of the newest daily planet build of Protomaps (only its header is read until an area is
    extracted from it)."""
    if fetch is None:
        def fetch(url):
            req = urllib.request.Request(url, headers={"User-Agent": pm.USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:     # noqa: S310 - fixed https address
                return r.read(1 << 20)
    try:
        data = json.loads(fetch(BUILDS_URL).decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - offline, or the list changed its form
        raise OfflineMapError("offline") from e
    keys = []
    for item in data if isinstance(data, list) else data.get("builds", []):
        key = item.get("key") if isinstance(item, dict) else item
        if isinstance(key, str) and key.endswith(".pmtiles"):
            keys.append(key)
    if not keys:
        raise OfflineMapError("no build")
    return BUILD_BASE + sorted(keys)[-1]


# ---- a preview server ---------------------------------------------------------------------------------------
class _RangeHandler(SimpleHTTPRequestHandler):
    """Static files with byte ranges, which PMTiles reading needs."""

    extensions_map = dict(SimpleHTTPRequestHandler.extensions_map, **{
        ".mjs": "text/javascript", ".js": "text/javascript", ".json": "application/json",
        ".pmtiles": "application/octet-stream", ".ttf": "font/ttf", ".wasm": "application/wasm",
        ".css": "text/css", ".html": "text/html; charset=utf-8"})

    def log_message(self, *_a):
        pass

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or os.path.isdir(path):
            return super().send_head()
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        if not m or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        a, b = m.groups()
        if a == "":
            start, end = max(0, size - int(b or 0)), size - 1
        else:
            start, end = int(a), min(int(b) if b else size - 1, size - 1)
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        f = open(path, "rb")     # noqa: SIM115 - closed by copyfile's caller
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._left = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        left = getattr(self, "_left", None)
        self._left = None
        try:
            if left is None:
                return super().copyfile(source, outputfile)
            while left > 0:
                buf = source.read(min(65536, left))
                if not buf:
                    break
                outputfile.write(buf)
                left -= len(buf)
        except (ConnectionError, OSError):     # the browser stopped reading (a closed tab, a cancelled tile)
            pass
        return None


_servers = {}


def serve(folder):
    """The address of a preview server for a site folder on this computer only (made once per folder)."""
    folder = os.path.abspath(folder)
    if folder in _servers:
        return _servers[folder][1]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_RangeHandler, directory=folder))
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, name="biruni-site-preview", daemon=True)
    t.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    _servers[folder] = (httpd, url)
    return url


def stop_all():
    for httpd, _url in list(_servers.values()):
        try:
            httpd.shutdown()
            httpd.server_close()
        except OSError:
            pass
    _servers.clear()
