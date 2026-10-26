"""One-click web map: a single self-contained .html file (Leaflet and the data
inlined) that opens in any browser, on a phone too, without a server. Only the
basemap tiles need the internet."""
import base64
import html
import json
import os

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeatureRequest, QgsGeometry,
                       QgsProject, QgsRectangle, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QDate, QDateTime, QTime

from .. import i18n
from ..i18n import tr
from ..version import APP_NAME
from .styles import ExpressionEvaluator, LayerStyler, geometry_kind, label_expression

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources", "web")
SIZE_WARNING_MB = 20
MAX_LABELS = 2000
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

BASEMAPS = {
    "osm": {"url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png", "maxZoom": 19,
            "attribution": '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'},
    "topo": {"url": "https://tile.opentopomap.org/{z}/{x}/{y}.png", "maxZoom": 17,
             "attribution": '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, '
                            'SRTM | &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)'},
    "none": None,
}


def json_value(v):
    """An attribute value as something json can write."""
    if v is None:
        return None
    if isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, QDateTime):
        return v.toString("yyyy-MM-dd HH:mm:ss") if v.isValid() else None
    if isinstance(v, QDate):
        return v.toString("yyyy-MM-dd") if v.isValid() else None
    if isinstance(v, QTime):
        return v.toString("HH:mm:ss") if v.isValid() else None
    if isinstance(v, (bytes, bytearray)) or type(v).__name__ == "QByteArray":
        return None
    if isinstance(v, (list, tuple)):
        return [json_value(x) for x in v]
    if isinstance(v, dict):
        return {str(k): json_value(x) for k, x in v.items()}
    return str(v)


def popup_fields(layer):
    """(index, display name) of the fields shown in popups: not hidden in the
    attribute form and not the internal primary key."""
    pk = set(layer.dataProvider().pkAttributeIndexes()) if layer.dataProvider() else set()
    out = []
    for i, f in enumerate(layer.fields()):
        if i in pk and layer.providerType() == "ogr":
            continue
        if layer.editorWidgetSetup(i).type() == "Hidden":
            continue
        out.append((i, layer.attributeDisplayName(i)))
    return out


def _layer_data(layer, project, simplify_m=0.0, labels=True, extent=None):
    kind = geometry_kind(layer)
    xform = QgsCoordinateTransform(layer.crs(), WGS84, project.transformContext())
    fields = popup_fields(layer)
    label_of = ExpressionEvaluator(layer, label_expression(layer) if labels else None)
    request = QgsFeatureRequest()
    if extent is not None:
        request.setFilterRect(extent)
    feats = []
    bounds = QgsRectangle()
    bounds.setNull()
    tol = simplify_m / 111320.0 if simplify_m else 0
    n_labels = 0
    with LayerStyler(layer) as styler:
        for f in layer.getFeatures(request):
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            s = styler.style_index(f)
            if s is None:
                continue
            g = QgsGeometry(g)
            if QgsWkbTypes.isCurvedType(g.wkbType()):
                g = QgsGeometry(g.constGet().segmentize())
            try:
                g.transform(xform)
            except Exception:
                continue
            if tol and kind != "point":
                sg = g.simplify(tol)
                if sg is not None and not sg.isEmpty():
                    g = sg
            bounds.combineExtentWith(g.boundingBox())
            props = {"s": s, "v": [json_value(f.attribute(i)) for i, _ in fields]}
            if label_of.expr is not None and n_labels < MAX_LABELS:
                lbl = label_of(f)
                if lbl:
                    props["l"] = lbl
                    n_labels += 1
            feats.append({"type": "Feature", "properties": props, "geometry": json.loads(g.asJson(6))})
        legend = styler.legend()
        styles = styler.styles
    data = {
        "name": layer.name(),
        "kind": kind,
        "fields": [name for _, name in fields],
        "styles": styles or [{}],
        "legend": legend,
        "geojson": {"type": "FeatureCollection", "features": feats},
    }
    return data, bounds


def export_web_map(layers, path, title="", basemap="osm", options=None):
    """Write `layers` (vector layers, top of the map first) to one .html file.

    options: simplify_m (metres, 0 = exact), labels (bool, default True),
    visible (set of layer ids shown at start; default all), extent
    (QgsReferencedRectangle limiting the features), project (QgsProject)."""
    options = options or {}
    project = options.get("project") or QgsProject.instance()
    visible = options.get("visible")
    extent = options.get("extent")
    out_layers = []
    total = QgsRectangle()
    total.setNull()
    for lyr in layers:
        if not isinstance(lyr, QgsVectorLayer) or not lyr.isSpatial() or geometry_kind(lyr) is None:
            continue
        rect = None
        if extent is not None:
            rect = QgsCoordinateTransform(extent.crs(), lyr.crs(), project.transformContext()) \
                .transformBoundingBox(extent)
        data, b = _layer_data(lyr, project, options.get("simplify_m", 0.0), options.get("labels", True), rect)
        data["visible"] = visible is None or lyr.id() in visible
        out_layers.append(data)
        if not b.isNull():
            total.combineExtentWith(b)
    if not out_layers:
        raise ValueError(tr("Choose at least one vector layer."))
    bm = BASEMAPS.get(basemap)
    payload = {
        "title": title,
        "layers": out_layers,
        "basemap": bm,
        "bounds": None if total.isNull() else [[total.yMinimum(), total.xMinimum()],
                                                [total.yMaximum(), total.xMaximum()]],
        "rtl": i18n.is_rtl(),
        "text": {"layers": tr("Layers"), "legend": tr("Legend"), "made": tr("Made with {app}").format(app=APP_NAME)},
    }
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    with open(os.path.join(WEB_DIR, "leaflet.js"), encoding="utf-8") as f:
        leaflet_js = f.read()
    with open(os.path.join(WEB_DIR, "leaflet.css"), encoding="utf-8") as f:
        leaflet_css = f.read()
    for img in ("layers-2x.png", "layers.png"):  # the layer switcher icon, inlined so the page works offline
        with open(os.path.join(WEB_DIR, img), "rb") as f:
            uri = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
        leaflet_css = leaflet_css.replace("images/" + img, uri)
    page = (TEMPLATE
            .replace("%%LANG%%", "ar" if i18n.is_rtl() else i18n.lang() or "en")
            .replace("%%DIR%%", "rtl" if i18n.is_rtl() else "ltr")
            .replace("%%TITLE%%", html.escape(title or APP_NAME))
            .replace("%%LEAFLET_CSS%%", leaflet_css)
            .replace("%%LEAFLET_JS%%", leaflet_js.replace("</script", "<\\/script"))
            .replace("%%DATA%%", blob))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    return path


def file_size_mb(path):
    return os.path.getsize(path) / (1024 * 1024)


TEMPLATE = r"""<!doctype html>
<html lang="%%LANG%%" dir="%%DIR%%">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="Biruni GIS">
<title>%%TITLE%%</title>
<style>%%LEAFLET_CSS%%</style>
<style>
html,body{height:100%;margin:0;font-family:"Segoe UI",Tahoma,Arial,sans-serif;background:#f4f6f8;color:#1d2733}
#bar{position:absolute;top:0;left:0;right:0;height:48px;display:flex;align-items:center;padding:0 16px;
 background:#14365a;color:#fff;font-size:17px;font-weight:600;z-index:1000;box-shadow:0 1px 4px rgba(0,0,0,.25);
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#map{position:absolute;top:48px;bottom:0;left:0;right:0;direction:ltr}
[dir=rtl] .legend,[dir=rtl] .leaflet-popup-content,[dir=rtl] .leaflet-control-layers{direction:rtl;text-align:right}
body.notitle #bar{display:none} body.notitle #map{top:0}
.leaflet-control-layers,.legend{font-size:13px;line-height:1.5}
.legend{background:#fff;padding:8px 10px;border-radius:6px;box-shadow:0 1px 5px rgba(0,0,0,.3);max-height:45vh;
 overflow:auto;max-width:260px}
.legend h4{margin:0 0 4px;font-size:13px}.legend .ly{margin:6px 0 2px;font-weight:600}
.legend .it{display:flex;align-items:center;gap:6px;margin:2px 0}
.legend svg{flex:none}
.pp table{border-collapse:collapse;font-size:12px;margin-top:4px}
.pp th{text-align:start;padding:2px 8px 2px 0;color:#5b6b7b;font-weight:600;vertical-align:top}
.pp td{padding:2px 0;word-break:break-word}
[dir=rtl] .pp th{padding:2px 0 2px 8px}
.lbl{background:transparent;border:0;box-shadow:none;font-weight:600;color:#1d2733;
 text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff}
.lbl:before{display:none}
</style>
</head>
<body>
<div id="bar"></div>
<div id="map"></div>
<script>%%LEAFLET_JS%%</script>
<script type="application/json" id="biruni-data">%%DATA%%</script>
<script>
(function(){
var D = JSON.parse(document.getElementById('biruni-data').textContent);
var bar = document.getElementById('bar');
if (D.title) bar.textContent = D.title; else document.body.className = 'notitle';
var side = D.rtl ? 'topleft' : 'topright', other = D.rtl ? 'bottomleft' : 'bottomright';
var map = L.map('map', {preferCanvas: true, zoomControl: false});
L.control.zoom({position: D.rtl ? 'topright' : 'topleft'}).addTo(map);
map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a> | ' + esc(D.text.made));
if (D.basemap) L.tileLayer(D.basemap.url, {maxZoom: D.basemap.maxZoom, attribution: D.basemap.attribution}).addTo(map);
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function swatch(kind, st){
  var stroke = st.stroke === false ? 'none' : st.color, sw = Math.min(st.weight || 1, 4);
  if (kind === 'point') return '<svg width="18" height="18"><circle cx="9" cy="9" r="'+Math.min(st.radius||5,7)+'" fill="'+st.fillColor+'" fill-opacity="'+st.fillOpacity+'" stroke="'+stroke+'" stroke-opacity="'+st.opacity+'" stroke-width="'+sw+'"/></svg>';
  if (kind === 'line') return '<svg width="22" height="18"><line x1="1" y1="9" x2="21" y2="9" stroke="'+st.color+'" stroke-opacity="'+st.opacity+'" stroke-width="'+Math.max(sw,2)+'"'+(st.dashArray?' stroke-dasharray="'+st.dashArray+'"':'')+'/></svg>';
  return '<svg width="22" height="16"><rect x="1" y="1" width="20" height="14" fill="'+st.fillColor+'" fill-opacity="'+st.fillOpacity+'" stroke="'+stroke+'" stroke-opacity="'+st.opacity+'" stroke-width="'+sw+'"/></svg>';
}
var groups = [];
for (var i = D.layers.length - 1; i >= 0; i--) {  // bottom layer first, so the first layer is drawn on top
  (function(ly){
    var styleOf = function(f){ return ly.styles[f.properties.s] || ly.styles[0]; };
    var g = L.geoJSON(ly.geojson, {
      style: styleOf,
      pointToLayer: function(f, ll){ return L.circleMarker(ll, styleOf(f)); },
      onEachFeature: function(f, l){
        var rows = '';
        ly.fields.forEach(function(name, k){
          var v = f.properties.v[k];
          if (v === null || v === undefined || v === '') return;
          rows += '<tr><th>' + esc(name) + '</th><td>' + esc(v) + '</td></tr>';
        });
        l.bindPopup('<div class="pp"><b>' + esc(ly.name) + '</b>' + (rows ? '<table>' + rows + '</table>' : '') + '</div>', {maxWidth: 320});
        if (f.properties.l) l.bindTooltip(esc(f.properties.l), {permanent: true, direction: 'center', className: 'lbl'});
      }
    });
    if (ly.visible) g.addTo(map);
    groups[i] = g;
  })(D.layers[i]);
}
var overlays = {};
D.layers.forEach(function(ly, i){ overlays['<span>' + esc(ly.name) + '</span><span hidden>' + i + '</span>'] = groups[i]; });
L.control.layers(null, overlays, {collapsed: window.innerWidth < 600, position: side}).addTo(map);
L.control.scale({imperial: false, position: other === 'bottomright' ? 'bottomleft' : 'bottomright'}).addTo(map);
var lg = L.control({position: other});
lg.onAdd = function(){
  var d = L.DomUtil.create('div', 'legend'), h = '<h4>' + esc(D.text.legend) + '</h4>';
  D.layers.forEach(function(ly){
    if (!ly.legend.length) return;
    h += '<div class="ly">' + esc(ly.name) + '</div>';
    ly.legend.forEach(function(it){ h += '<div class="it">' + swatch(ly.kind, it.style) + '<span>' + esc(it.label || '') + '</span></div>'; });
  });
  d.innerHTML = h;
  L.DomEvent.disableClickPropagation(d); L.DomEvent.disableScrollPropagation(d);
  return d;
};
lg.addTo(map);
if (D.bounds) {
  var b = L.latLngBounds(D.bounds);
  if (b.getNorthEast().equals(b.getSouthWest())) map.setView(b.getCenter(), 16); else map.fitBounds(b, {padding: [20, 20]});
} else map.setView([20, 0], 2);
})();
</script>
</body>
</html>
"""
