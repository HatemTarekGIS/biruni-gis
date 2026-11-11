"""Publish a map as a small website folder - ready to drag onto Netlify Drop or
to push to a GitHub Pages repository. Nothing is uploaded from here.

Two kinds of site:
* "inline": index.html is the one-file web map (Leaflet and the data inside).
  Works from the disk, from a USB stick and from any web host.
* "tiles": every layer is written as one PMTiles file (vector tiles, read in
  pieces over HTTP range requests) and index.html draws them with MapLibre.
  For big layers - hundreds of thousands of features stay fast - but the site
  must be served over http(s) (GitHub Pages and Netlify do range requests, and
  offline_maps.serve previews it on this computer); opening index.html from the
  disk shows nothing. MapLibre, pmtiles.js and the Arabic font are copied into
  lib/ (no CDN), labels are drawn in Arabic, and a basemap pack (PMTiles) can be
  cut to the area of the data and carried in the site: then it needs no internet
  at all.
"auto" picks "tiles" when the inline page would pass SIZE_LIMIT_MB, or when a
basemap pack is given.

The folder also gets a README with the three publishing steps, a .nojekyll
file (GitHub Pages serves files as they are) and, optionally, the portfolio
page at portfolio/index.html with a link from the map.
"""
import html
import json
import os
import shutil
import tempfile

from qgis.core import QgsProject, QgsVectorLayer

from .. import i18n
from ..i18n import tr
from ..version import APP_NAME
from .styles import geometry_kind
from .webmap import _layer_data, export_web_map, file_size_mb

SIZE_LIMIT_MB = 25           # inline pages above this are slow to open
GITHUB_FILE_LIMIT_MB = 100   # GitHub refuses larger files; it warns from 50 MB
MAPLIBRE_VERSION = "6.16.0"     # resources/web/maplibre (BSD-3-Clause)
PMTILES_VERSION = "4.5.0"       # resources/web/pmtiles (BSD-3-Clause)
BASEMAP_MARGIN = 0.25           # the basemap is cut to the data's extent and this much around it
BASEMAP_MAX_ZOOM = 15


def safe_file_name(name, used):
    base = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_")[:40] or "layer"
    cand, k = base, 2
    while cand.lower() in used:
        cand, k = f"{base}_{k}", k + 1
    used.add(cand.lower())
    return cand


def publish_site(layers, out_dir, title="", mode="auto", basemap="osm", portfolio=None, project=None,
                 max_zoom=16, basemap_pack=None, progress=None):
    """Write the website into out_dir (created; existing files of the same names are replaced).

    portfolio: optional dict(title=, author=, description=, stats=) - also writes portfolio/index.html.
    basemap_pack: a PMTiles basemap (offline_maps): its tiles around the data go into the site.
    Returns dict(index, mode, files: [(relative path, MB)], warnings: [text])."""
    project = project or QgsProject.instance()
    layers = [lyr for lyr in layers if isinstance(lyr, QgsVectorLayer) and lyr.isSpatial()
              and geometry_kind(lyr) is not None]
    if not layers:
        raise ValueError(tr("Choose at least one vector layer."))
    os.makedirs(out_dir, exist_ok=True)
    title = title or project.title() or APP_NAME
    index = os.path.join(out_dir, "index.html")
    if basemap_pack and mode == "auto":
        mode = "tiles"
    if mode in ("auto", "inline"):
        export_web_map(layers, index, title, basemap, {"project": project})
        if mode == "auto" and file_size_mb(index) > SIZE_LIMIT_MB:
            mode = "tiles"
        else:
            mode = "inline"
    if mode == "tiles":
        _tiles_site(layers, out_dir, index, title, project, max_zoom, basemap, basemap_pack, progress)
    has_portfolio = False
    if portfolio is not None:
        from ..portfolio import export_portfolio

        export_portfolio(project, os.path.join(out_dir, "portfolio"), portfolio.get("title") or title,
                         portfolio.get("author", ""), portfolio.get("description", ""), portfolio.get("stats"))
        has_portfolio = True
        _add_portfolio_link(index)
    with open(os.path.join(out_dir, ".nojekyll"), "w", encoding="utf-8") as f:
        f.write("")
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(readme_text(title, mode, has_portfolio))
    files, warnings = site_files(out_dir)
    return {"index": index, "mode": mode, "files": files, "warnings": warnings}


def site_files(out_dir):
    """[(relative path, MB)] of the site, and warnings for files hosts refuse or that load slowly."""
    files, warnings = [], []
    for root, _dirs, names in os.walk(out_dir):
        for n in names:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, out_dir).replace("\\", "/")
            mb = os.path.getsize(p) / (1024 * 1024)
            files.append((rel, round(mb, 2)))
            if mb > GITHUB_FILE_LIMIT_MB:
                warnings.append(tr("{f} is {m:.0f} MB: GitHub does not accept files over 100 MB - use Netlify or "
                                   "simplify the data.").format(f=rel, m=mb))
            elif mb > 50:
                warnings.append(tr("{f} is {m:.0f} MB: GitHub warns above 50 MB.").format(f=rel, m=mb))
    files.sort()
    return files, warnings


def readme_text(title, mode, has_portfolio):
    serve = ("" if mode == "inline" else
             tr("This site reads its layers in pieces from the server, so open it from the web address "
                "(not by double-clicking index.html).") + "\n\n")
    port = (tr("The portfolio page is at portfolio/index.html.") + "\n\n") if has_portfolio else ""
    return (f"# {title}\n\n" + tr("Made with {app}.").format(app=APP_NAME) + "\n\n" + serve + port +
            "## " + tr("Put it on the internet in three steps") + "\n\n" +
            "**Netlify (" + tr("no account needed to try") + ")**\n\n" +
            "1. " + tr("Open https://app.netlify.com/drop in a browser.") + "\n" +
            "2. " + tr("Drag this whole folder onto the page.") + "\n" +
            "3. " + tr("Copy the web address it gives you and share it.") + "\n\n" +
            "**GitHub Pages**\n\n" +
            "1. " + tr("Create a public repository on GitHub and upload the files of this folder (Add file → "
                       "Upload files).") + "\n" +
            "2. " + tr("In the repository: Settings → Pages → Branch: main, folder: / (root) → Save.") + "\n" +
            "3. " + tr("After a minute the map is at https://<your name>.github.io/<repository>/.") + "\n")


def _add_portfolio_link(index):
    with open(index, encoding="utf-8") as f:
        page = f.read()
    link = (f'<a href="portfolio/index.html" style="position:fixed;bottom:22px;inset-inline-start:10px;z-index:1000;'
            f'background:#0E7C74;color:#fff;padding:6px 12px;border-radius:16px;font:14px Segoe UI,Tahoma,sans-serif;'
            f'text-decoration:none">{html.escape(tr("Portfolio"))}</a>')
    page = page.replace("</body>", link + "</body>", 1)
    with open(index, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)


# ---- tiles site ------------------------------------------------------------------------
def _write_pmtiles(geojson, path, name, max_zoom):
    """GeoJSON FeatureCollection (WGS 84) -> PMTiles vector tiles with one layer `name`."""
    from osgeo import gdal

    tmp = tempfile.mkdtemp(prefix="biruni_site_")
    try:
        src = os.path.join(tmp, "layer.geojson")
        with open(src, "w", encoding="utf-8") as f:
            json.dump(geojson, f, ensure_ascii=False)
        if os.path.exists(path):
            os.remove(path)
        opts = gdal.VectorTranslateOptions(format="PMTiles", layerName=name, datasetCreationOptions=[
            "MINZOOM=0", f"MAXZOOM={max_zoom}", f"NAME={name}"])
        ds = gdal.VectorTranslate(path, src, options=opts)
        if ds is None:
            raise RuntimeError(tr("Could not write {p}").format(p=path))
        ds = None  # noqa: F841 - flush
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _paint(kind, styles, prefix):
    """MapLibre layers drawing the Biruni style index property `_s`."""
    def pick(attr, default):
        if len(styles) == 1:
            return styles[0].get(attr, default)
        expr = ["match", ["get", "_s"]]
        for i, st in enumerate(styles):
            expr += [i, st.get(attr, default)]
        return expr + [default]

    if kind == "polygon":
        return [{"id": prefix + "-fill", "type": "fill", "paint": {
                    "fill-color": pick("fillColor", "#3388ff"), "fill-opacity": pick("fillOpacity", 0.25)}},
                {"id": prefix + "-line", "type": "line", "paint": {
                    "line-color": pick("color", "#3388ff"), "line-width": pick("weight", 1)}}]
    if kind == "line":
        return [{"id": prefix + "-line", "type": "line", "paint": {
            "line-color": pick("color", "#3388ff"), "line-width": pick("weight", 2),
            "line-opacity": pick("opacity", 1)}}]
    return [{"id": prefix + "-circle", "type": "circle", "paint": {
        "circle-color": pick("fillColor", "#3388ff"), "circle-radius": pick("radius", 5),
        "circle-stroke-color": pick("color", "#ffffff"), "circle-stroke-width": 1}}]


def _labels(kind, prefix, color="#222222"):
    """A MapLibre symbol layer for the labels a feature carries in `_l`, in the bundled Arabic font."""
    from .offline_maps import FONT_NAME

    layout = {"text-field": ["get", "_l"], "text-font": [FONT_NAME], "text-size": 13,
              "text-max-width": 10, "text-allow-overlap": False}
    if kind == "line":
        layout["symbol-placement"] = "line"
    elif kind == "point":
        layout.update({"text-anchor": "top", "text-offset": [0, 0.7]})
    return {"id": prefix + "-label", "type": "symbol", "filter": ["has", "_l"], "layout": layout,
            "paint": {"text-color": color, "text-halo-color": "#ffffff", "text-halo-width": 1.5}}


def _cut_basemap(pack, out, bounds, progress=None):
    """The tiles of a basemap pack around the data, into out. None when the pack has nothing there."""
    from . import pmtiles as pm

    if bounds is None:
        return None
    w, s, e, n = bounds
    mx = max((e - w) * BASEMAP_MARGIN, 0.01)
    my = max((n - s) * BASEMAP_MARGIN, 0.01)
    bbox = (max(w - mx, -180.0), max(s - my, -85.05), min(e + mx, 180.0), min(n + my, 85.05))
    try:
        return pm.extract(pack, out, bbox, 0, BASEMAP_MAX_ZOOM, progress=progress)
    except pm.PMTilesError:
        return None


def _tiles_site(layers, out_dir, index, title, project, max_zoom, basemap="osm", basemap_pack=None,
                progress=None):
    from . import offline_maps as om

    data_dir = os.path.join(out_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    used, entries = set(), []
    bounds = None
    for lyr in layers:
        data, b = _layer_data(lyr, project, 0.0, True, None)
        fname = safe_file_name(lyr.name(), used)
        feats = []
        for ft in data["geojson"]["features"]:
            props = {"_s": ft["properties"]["s"]}
            if ft["properties"].get("l"):
                props["_l"] = str(ft["properties"]["l"])
            for name, v in zip(data["fields"], ft["properties"]["v"]):
                if v is not None:
                    props[name] = v if isinstance(v, (int, float, str, bool)) else str(v)
            feats.append({"type": "Feature", "properties": props, "geometry": ft["geometry"]})
        if not feats:
            continue
        _write_pmtiles({"type": "FeatureCollection", "features": feats}, os.path.join(data_dir, fname + ".pmtiles"),
                       fname, max_zoom)
        entries.append({"name": lyr.name(), "file": f"data/{fname}.pmtiles", "source_layer": fname,
                        "layers": _paint(data["kind"], data["styles"] or [{}], fname) + [_labels(data["kind"], fname)],
                        "legend": data["legend"], "fields": data["fields"]})
        if not b.isNull():
            bounds = [b.xMinimum(), b.yMinimum(), b.xMaximum(), b.yMaximum()] if bounds is None else [
                min(bounds[0], b.xMinimum()), min(bounds[1], b.yMinimum()), max(bounds[2], b.xMaximum()),
                max(bounds[3], b.yMaximum())]
    base = None
    if basemap_pack:
        if _cut_basemap(basemap_pack, os.path.join(data_dir, "basemap.pmtiles"), bounds, progress) is not None:
            base = {"file": "data/basemap.pmtiles", "layers": om.web_basemap_layers(),
                    "attribution": om.OSM_ATTRIBUTION}
    om.copy_web_assets(out_dir, basemap=base is not None)
    payload = {"title": title, "layers": entries, "bounds": bounds, "rtl": i18n.is_rtl(),
               "basemap": base, "online": basemap != "none" and base is None,
               "fontFaces": om.font_faces(), "osm": om.OSM_ATTRIBUTION,
               "text": {"layers": tr("Layers"), "made": tr("Made with {app}").format(app=APP_NAME),
                        "basemap": tr("Basemap"),
                        "nohttp": tr("This map reads its data in pieces from a web server: open it from its web "
                                     "address (or with Preview Website), not by double-clicking index.html.")}}
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = (TILES_TEMPLATE.replace("%%LANG%%", "ar" if i18n.is_rtl() else i18n.lang() or "en")
            .replace("%%DIR%%", "rtl" if i18n.is_rtl() else "ltr")
            .replace("%%TITLE%%", html.escape(title))
            .replace("%%DATA%%", blob))
    with open(index, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)


TILES_TEMPLATE = r"""<!doctype html>
<html lang="%%LANG%%" dir="%%DIR%%"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%%TITLE%%</title>
<link rel="stylesheet" href="lib/maplibre-gl.css">
<script src="lib/pmtiles.js"></script>
<style>
html,body,#map{margin:0;height:100%;font:14px "Segoe UI",Tahoma,Arial,sans-serif}
#panel{position:absolute;top:10px;inset-inline-start:10px;background:#fff;border-radius:8px;padding:8px 12px;
 box-shadow:0 1px 4px rgba(0,0,0,.3);max-width:260px;max-height:70%;overflow:auto;z-index:2}
#panel h1{font-size:16px;margin:0 0 6px}#panel label{display:block;margin:3px 0}
#made{position:absolute;bottom:4px;inset-inline-start:8px;font-size:11px;color:#555;z-index:2}
#nohttp{display:none;position:absolute;inset:0;background:#fff;padding:2em;z-index:9;font-size:16px}
.maplibregl-popup-content table{border-collapse:collapse}.maplibregl-popup-content th{text-align:start;padding:1px 6px}
</style></head><body><div id="map"></div><div id="panel"></div><div id="made"></div>
<div id="nohttp"></div>
<script type="module">
import * as maplibregl from "./lib/maplibre-gl.mjs";
const D = %%DATA%%;
if (location.protocol === "file:") { const w = document.getElementById("nohttp"); w.textContent = D.text.nohttp; w.style.display = "block"; }
const protocol = new pmtiles.Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);
const base = new URL(".", location.href).href;
const faces = {};
for (const [name, list] of Object.entries(D.fontFaces)) faces[name] = list.map(f => ({...f, url: base + f.url}));
const style = {version: 8, "font-faces": faces, sources: {}, layers: [
  {id: "background", type: "background", paint: {"background-color": "#f4f2ee"}}]};
if (D.basemap) {
  style.sources.basemap = {type: "vector", url: "pmtiles://" + base + D.basemap.file, attribution: D.basemap.attribution};
  style.layers = D.basemap.layers;
} else if (D.online) {
  style.sources.osm = {type: "raster", tileSize: 256, maxzoom: 19,
    tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], attribution: D.osm};
  style.layers.push({id: "osm", type: "raster", source: "osm"});
}
const map = new maplibregl.Map({container: "map", style: style, center: [0, 20], zoom: 2});
map.addControl(new maplibregl.NavigationControl(), D.rtl ? "top-left" : "top-right");
map.addControl(new maplibregl.ScaleControl());
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const panel = document.getElementById("panel");
panel.innerHTML = "<h1>" + esc(D.title) + "</h1>";
document.getElementById("made").textContent = D.text.made;
map.on("load", () => {
  D.layers.forEach((L, i) => {
    const src = "src" + i;
    map.addSource(src, {type: "vector", url: "pmtiles://" + base + L.file});
    const ids = [];
    L.layers.forEach(ml => { ml.source = src; ml["source-layer"] = L.source_layer; map.addLayer(ml); ids.push(ml.id); });
    const lab = document.createElement("label");
    const box = document.createElement("input"); box.type = "checkbox"; box.checked = true;
    box.onchange = () => ids.forEach(id => map.setLayoutProperty(id, "visibility", box.checked ? "visible" : "none"));
    lab.appendChild(box); lab.appendChild(document.createTextNode(" " + L.name)); panel.appendChild(lab);
    ids.filter(id => !id.endsWith("-label")).forEach(id => {
      map.on("click", id, e => {
        const p = e.features[0].properties; let rows = "";
        L.fields.forEach(f => { if (p[f] !== undefined && p[f] !== null && p[f] !== "") rows += "<tr><th>" + esc(f) + "</th><td>" + esc(p[f]) + "</td></tr>"; });
        new maplibregl.Popup().setLngLat(e.lngLat).setHTML("<b>" + esc(L.name) + "</b><table>" + rows + "</table>").addTo(map);
      });
      map.on("mouseenter", id, () => map.getCanvas().style.cursor = "pointer");
      map.on("mouseleave", id, () => map.getCanvas().style.cursor = "");
    });
  });
  if (D.bounds) map.fitBounds([[D.bounds[0], D.bounds[1]], [D.bounds[2], D.bounds[3]]], {padding: 30, duration: 0});
});
</script></body></html>
"""
