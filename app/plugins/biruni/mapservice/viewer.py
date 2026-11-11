"""The viewer page of a map service folder (index.html): the map from the one PMTiles archive, identify and
attribute / area queries read from the FlatGeobuf files in pieces (HTTP range requests), and the signature of
the folder checked in the browser (Web Crypto: SHA-256 and Ed25519).

Everything it needs is in the folder (lib/): it works on a network with no internet. Only the online basemap,
when chosen, comes from the internet.
"""
import html
import json
import os

from ..i18n import tr
from ..version import APP_NAME

LOGO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "branding", "logo.svg")


def logo_svg():
    try:
        with open(LOGO, encoding="utf-8") as f:
            svg = f.read()
    except OSError:
        return ""
    svg = svg[svg.find("<svg"):]
    # a unique gradient id (the page may carry other SVGs) and a size set by CSS
    return (svg.replace('id="bg"', 'id="bmsLogoBg"').replace("url(#bg)", "url(#bmsLogoBg)")
            .replace('width="256" height="256"', 'class="logo" aria-hidden="true"'))


def texts():
    return {
        "layers": tr("Layers"), "query": tr("Query"), "layer": tr("Layer"), "field": tr("Field"),
        "cond": tr("Condition"), "value": tr("Value"), "inview": tr("Only in the current view"),
        "run": tr("Run"), "clear": tr("Clear"),
        "ops": {"eq": tr("equals"), "ne": tr("is not"), "has": tr("contains"), "starts": tr("starts with"),
                "gt": tr("greater than"), "lt": tr("less than")},
        "found": tr("{n} found ({m} read)"), "first": tr("Showing the first {n}."), "none": tr("Nothing found."),
        "reading": tr("Reading…"), "features": tr("features"), "zoom": tr("Zoom to"),
        "readfail": tr("Could not read the data: {e}"),
        "checking": tr("Checking the signature…"), "signed": tr("Signed by {name}"), "key": tr("Key {fp}"),
        "checked": tr("{n} files checked"), "checkall": tr("Check all files"),
        "altered": tr("Altered after signing: {files}"), "unsigned": tr("Not signed"),
        "nocheck": tr("This browser cannot check the signature here (it needs https, or a newer browser)."),
        "otherkey": tr("Signed by a different key than the link expects."),
        "badsig": tr("The signature does not match: do not trust this copy."),
        "made": tr("Made with {app}").format(app=APP_NAME), "ogc": tr("OGC API"), "tilejson": "TileJSON",
        "signature": tr("Signature"), "panel": tr("Panel"),
        "nowebgl": tr("This browser cannot draw the map (WebGL is off). The layers, the queries and the signature "
                      "still work."),
        "nohttp": tr("This map reads its data in pieces from a web server: open it from its web address (or with "
                     "Preview Website), not by double-clicking index.html."),
    }


def write(path, title, description, entries, service, rtl=False, lang="en"):
    from ..share import offline_maps as om

    data = {"title": title, "description": description, "rtl": bool(rtl), "service": service,
            "layers": [{k: e[k] for k in ("id", "name", "kind", "fields", "fgb", "bbox", "layers", "legend", "count")}
                       for e in entries],
            "fontFaces": om.font_faces(), "osm": om.OSM_ATTRIBUTION, "text": texts()}
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = (TEMPLATE.replace("%%LANG%%", html.escape(lang))
            .replace("%%DIR%%", "rtl" if rtl else "ltr")
            .replace("%%TITLE%%", html.escape(title))
            .replace("%%LOGO%%", logo_svg())
            .replace("%%DATA%%", blob))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    return path


TEMPLATE = r"""<!doctype html>
<html lang="%%LANG%%" dir="%%DIR%%"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="Biruni GIS">
<title>%%TITLE%%</title>
<link rel="stylesheet" href="lib/maplibre-gl.css">
<script src="lib/pmtiles.js"></script>
<script src="lib/flatgeobuf.js"></script>
<style>
:root{--teal:#0B4F4A;--teal2:#14A394;--gold:#FBBF24;--ink:#14212b;--mute:#5b6b76;--line:#dfe6e9;--card:#fff;--bg:#f4f6f5}
*{box-sizing:border-box}
html,body{margin:0;height:100%;font:14px "Segoe UI",Tahoma,"Noto Sans Arabic",Arial,sans-serif;color:var(--ink);background:var(--bg)}
header{position:absolute;top:0;left:0;right:0;height:54px;display:flex;align-items:center;gap:10px;padding:0 14px;
 background:linear-gradient(90deg,var(--teal),var(--teal2));color:#fff;z-index:5;box-shadow:0 2px 6px rgba(0,0,0,.2)}
[dir=rtl] header{background:linear-gradient(270deg,var(--teal),var(--teal2))}
.logo{width:34px;height:34px;flex:none;display:block;border-radius:8px;box-shadow:0 0 0 1px rgba(255,255,255,.25)}
header h1{font-size:17px;margin:0;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0;unicode-bidi:plaintext}
#badge{flex:none;display:flex;align-items:center;gap:6px;border-radius:999px;padding:5px 11px;font-size:12.5px;background:rgba(255,255,255,.14);
 border:1px solid rgba(255,255,255,.28);cursor:pointer;max-width:46vw;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#badge i{width:9px;height:9px;border-radius:50%;background:#cbd5e1;flex:none}
#badge.ok i{background:#34d399}#badge.bad{background:#b91c1c;border-color:#fecaca}#badge.bad i{background:#fff}#badge.warn i{background:var(--gold)}
#panelBtn{display:none;flex:none;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.3);color:#fff;border-radius:8px;padding:6px 10px;font:inherit}
#map{position:absolute;top:54px;bottom:0;left:0;right:0;direction:ltr}
#panel{position:absolute;top:66px;inset-inline-start:12px;width:300px;max-height:calc(100% - 84px);overflow:auto;background:var(--card);
 border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,.18);z-index:4;display:flex;flex-direction:column}
.tabs{display:flex;border-bottom:1px solid var(--line)}
.tabs button{flex:1;border:0;background:none;padding:10px;font:inherit;font-weight:600;color:var(--mute);cursor:pointer;border-bottom:3px solid transparent}
.tabs button.on{color:var(--teal);border-bottom-color:var(--gold)}
.pane{padding:10px 12px}.pane.hide{display:none}
.lyr{display:flex;align-items:center;gap:8px;margin:5px 0}.lyr label{flex:1;unicode-bidi:plaintext}.lyr small{color:var(--mute)}
.leg{margin:2px 0 8px;padding-inline-start:26px;font-size:12.5px;color:var(--mute)}.leg div{display:flex;align-items:center;gap:6px;margin:2px 0}
.sw{width:14px;height:14px;border-radius:3px;border:1px solid rgba(0,0,0,.25);flex:none}
.pane label.f{display:block;font-size:12px;color:var(--mute);margin:8px 0 3px}
.pane select,.pane input[type=text]{width:100%;padding:7px 8px;border:1px solid var(--line);border-radius:8px;font:inherit;background:#fff}
.row{display:flex;gap:8px;margin-top:10px;align-items:center}
.btn{border:0;border-radius:8px;padding:8px 14px;font:inherit;font-weight:600;cursor:pointer;background:var(--teal);color:#fff}
.btn.alt{background:#e9efee;color:var(--ink)}
#qmsg{margin:10px 0 4px;color:var(--mute);font-size:12.5px}
#qres{list-style:none;margin:0;padding:0}#qres li{padding:6px 8px;border-radius:7px;cursor:pointer;unicode-bidi:plaintext}
#qres li:hover{background:#eef6f5}
footer{border-top:1px solid var(--line);padding:8px 12px;font-size:12px;color:var(--mute);display:flex;flex-wrap:wrap;gap:10px}
footer a{color:var(--teal)}
#sig{position:absolute;top:62px;inset-inline-end:12px;width:min(360px,calc(100% - 24px));background:#fff;border-radius:12px;box-shadow:0 6px 22px rgba(0,0,0,.22);
 padding:12px 14px;z-index:6;font-size:13px;line-height:1.6}#sig.hide{display:none}#sig code{direction:ltr;unicode-bidi:embed;font-size:12px}
#sig ul{margin:4px 0;padding-inline-start:18px;color:#b91c1c}
#nohttp{display:none;position:absolute;inset:54px 0 0 0;background:#fff;padding:2em;z-index:9;font-size:16px}
.maplibregl-popup-content{font:13px "Segoe UI",Tahoma,sans-serif;max-height:300px;overflow:auto;direction:%%DIR%%}
.maplibregl-popup-content table{border-collapse:collapse}.maplibregl-popup-content th{text-align:start;padding:2px 8px;color:var(--mute);font-weight:600}
.maplibregl-popup-content td{padding:2px 8px;unicode-bidi:plaintext}.maplibregl-popup-content h4{margin:0 0 6px;color:var(--teal)}
@media (max-width:700px){#panelBtn{display:block}#panel{width:calc(100% - 24px);max-height:55%;top:auto;bottom:12px}#panel.hide{display:none}#badge{max-width:38vw}}
</style></head><body>
<header>%%LOGO%%<h1 id="title"></h1><button id="panelBtn" type="button"></button><div id="badge" role="button" tabindex="0"><i></i><span></span></div></header>
<div id="map"></div>
<div id="panel"><div class="tabs"><button type="button" id="tabL" class="on"></button><button type="button" id="tabQ"></button></div>
 <div class="pane" id="paneL"></div>
 <div class="pane hide" id="paneQ">
  <label class="f" id="lLayer"></label><select id="qLayer"></select>
  <label class="f" id="lField"></label><select id="qField"></select>
  <label class="f" id="lCond"></label><select id="qOp"></select>
  <label class="f" id="lValue"></label><input type="text" id="qVal">
  <div class="row"><input type="checkbox" id="qView"><label for="qView" id="lView"></label></div>
  <div class="row"><button class="btn" type="button" id="qRun"></button><button class="btn alt" type="button" id="qClear"></button></div>
  <div id="qmsg"></div><ul id="qres"></ul>
 </div>
 <footer id="foot"></footer></div>
<div id="sig" class="hide"></div>
<div id="nohttp"></div>
<script type="module">
import * as maplibregl from "./lib/maplibre-gl.mjs";
const D = %%DATA%%, T = D.text, S = D.service, $ = id => document.getElementById(id);
const fmt = (s, o) => String(s).replace(/\{(\w+)\}/g, (m, k) => o[k] != null ? o[k] : m);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const base = new URL(".", location.href).href;
$("title").textContent = D.title; document.title = D.title;
$("tabL").textContent = T.layers; $("tabQ").textContent = T.query; $("panelBtn").textContent = T.panel;
$("lLayer").textContent = T.layer; $("lField").textContent = T.field; $("lCond").textContent = T.cond;
$("lValue").textContent = T.value; $("lView").textContent = T.inview; $("qRun").textContent = T.run; $("qClear").textContent = T.clear;
if (location.protocol === "file:") { const w = $("nohttp"); w.textContent = T.nohttp; w.style.display = "block"; }
$("panelBtn").onclick = () => $("panel").classList.toggle("hide");
for (const [b, p] of [["tabL", "paneL"], ["tabQ", "paneQ"]]) $(b).onclick = () => {
  for (const x of ["tabL", "tabQ"]) $(x).classList.toggle("on", x === b);
  for (const x of ["paneL", "paneQ"]) $(x).classList.toggle("hide", x !== p); };
// ---- footer
const foot = $("foot"), links = [];
if (S.ogc) links.push(`<a href="${esc(S.ogc)}" target="_blank" rel="noopener">${esc(T.ogc)}</a>`);
if (S.tilejson) links.push(`<a href="${esc(S.tilejson)}" target="_blank" rel="noopener">${esc(T.tilejson)}</a>`);
links.push(`<a href="manifest.json" target="_blank" rel="noopener">${esc(T.signature)}</a>`);
foot.innerHTML = links.join(" · ") + `<span style="margin-inline-start:auto">${esc(T.made)}</span>`;
// ---- the map
const protocol = new pmtiles.Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);
const faces = {};
for (const [name, list] of Object.entries(D.fontFaces)) faces[name] = list.map(f => ({...f, url: base + f.url}));
const style = {version: 8, "font-faces": faces, sources: {}, layers: [{id: "background", type: "background", paint: {"background-color": "#eef1ef"}}]};
if (S.basemap) { style.sources.basemap = {type: "vector", url: "pmtiles://" + base + S.basemap.file, attribution: S.basemap.attribution}; style.layers = S.basemap.layers; }
else if (S.online_basemap) { style.sources.osm = {type: "raster", tileSize: 256, maxzoom: 19, tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], attribution: D.osm};
  style.layers.push({id: "osm", type: "raster", source: "osm"}); }
/* without WebGL (an old phone, a locked-down PC) the map cannot be drawn: the queries and the signature still work */
let map = null;
try { map = new maplibregl.Map({container: "map", style: style, center: [0, 20], zoom: 2, hash: false}); }
catch (err) { $("map").innerHTML = `<div style="padding:2em;font-size:15px;direction:${D.rtl ? "rtl" : "ltr"}">${esc(T.nowebgl)}</div>`; }
const M = map || {on() {}, addControl() {}, getSource() { return null; }, getCanvas() { return {style: {}}; }};
window.BMS = {map: map, D: D};
if (map) { M.addControl(new maplibregl.NavigationControl(), D.rtl ? "top-left" : "top-right"); M.addControl(new maplibregl.ScaleControl()); }
const byId = {}, drawIds = [];
D.layers.forEach(L => { L.ids = []; byId[L.id] = L; });
const HL = {type: "FeatureCollection", features: []};
M.on("load", () => {
  if (S.raster) { map.addSource("raster", {type: "raster", url: "pmtiles://" + base + S.raster.file, tileSize: 256});
    map.addLayer({id: "raster", type: "raster", source: "raster"}); }
  if (S.tiles) map.addSource("svc", {type: "vector", url: "pmtiles://" + base + S.tiles.file});
  [...D.layers].reverse().forEach(L => { L.layers.forEach(ml => { ml.source = "svc"; ml["source-layer"] = L.id; map.addLayer(ml); L.ids.push(ml.id);
    if (!ml.id.endsWith("-label")) drawIds.push(ml.id); }); });
  map.addSource("hl", {type: "geojson", data: HL});
  map.addLayer({id: "hl-fill", type: "fill", source: "hl", filter: ["==", ["geometry-type"], "Polygon"], paint: {"fill-color": "#FBBF24", "fill-opacity": .35}});
  map.addLayer({id: "hl-line", type: "line", source: "hl", paint: {"line-color": "#B45309", "line-width": 3}});
  map.addLayer({id: "hl-pt", type: "circle", source: "hl", filter: ["==", ["geometry-type"], "Point"], paint: {"circle-radius": 8, "circle-color": "#FBBF24", "circle-stroke-color": "#B45309", "circle-stroke-width": 2}});
  if (S.bounds) map.fitBounds([[S.bounds[0], S.bounds[1]], [S.bounds[2], S.bounds[3]]], {padding: 40, duration: 0});
  window.BMS.ready = true;
});
{ const pane = $("paneL");
  D.layers.forEach(L => {
    const row = document.createElement("div"); row.className = "lyr";
    const box = document.createElement("input"); box.type = "checkbox"; box.checked = true; box.id = "v_" + L.id;
    box.onchange = () => map && L.ids.forEach(id => map.setLayoutProperty(id, "visibility", box.checked ? "visible" : "none"));
    const lab = document.createElement("label"); lab.htmlFor = box.id; lab.textContent = L.name;
    const n = document.createElement("small"); n.textContent = L.count.toLocaleString();
    row.append(box, lab, n); pane.appendChild(row);
    if (L.legend && L.legend.length > 1) { const lg = document.createElement("div"); lg.className = "leg";
      L.legend.slice(0, 12).forEach(it => { const d = document.createElement("div"), s = document.createElement("span"); s.className = "sw";
        s.style.background = (it.style && (it.style.fillColor || it.style.color)) || "#3388ff"; d.append(s, document.createTextNode(it.label || "")); lg.appendChild(d); });
      pane.appendChild(lg); }
  });
  if (S.raster) { const row = document.createElement("div"); row.className = "lyr"; const box = document.createElement("input"); box.type = "checkbox"; box.checked = true; box.id = "v_raster";
    box.onchange = () => map && map.setLayoutProperty("raster", "visibility", box.checked ? "visible" : "none");
    const lab = document.createElement("label"); lab.htmlFor = "v_raster"; lab.textContent = S.raster.name; row.append(box, lab); pane.appendChild(row); }
}
M.on("mousemove", e => { const f = drawIds.length && map.queryRenderedFeatures(e.point, {layers: drawIds}); map.getCanvas().style.cursor = f && f.length ? "pointer" : ""; });
// ---- reading the FlatGeobuf files in pieces
function url(L) { return base + L.fgb; }
async function* read(L, rect) { yield* flatgeobuf.deserialize(url(L), rect || {minX: -180, minY: -90, maxX: 180, maxY: 90}); }
function bboxOf(g) { let b = [Infinity, Infinity, -Infinity, -Infinity];
  const walk = c => { if (typeof c[0] === "number") { b[0] = Math.min(b[0], c[0]); b[1] = Math.min(b[1], c[1]); b[2] = Math.max(b[2], c[0]); b[3] = Math.max(b[3], c[1]); } else c.forEach(walk); };
  walk(g.coordinates); return b; }
function highlight(feats) { HL.features = feats.map(f => ({type: "Feature", properties: {}, geometry: f.geometry})); const s = M.getSource("hl"); if (s) s.setData(HL); }
function table(L, p) { let rows = ""; for (const k of Object.keys(p)) { if (k === "_fid") continue; const v = p[k];
    if (v === null || v === undefined || v === "") continue; rows += `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`; }
  return `<h4>${esc(L.name)}</h4><table>${rows}</table>`; }
let popup = null;
function show(L, f, at) { window.BMS.lastPopup = f.properties; if (!map) return; if (popup) popup.remove(); highlight([f]);
  popup = new maplibregl.Popup({maxWidth: "340px"}).setLngLat(at).setHTML(table(L, f.properties)).addTo(map); window.BMS.lastPopup = f.properties; }
async function identify(e) {
  if (!drawIds.length) return;
  const hits = map.queryRenderedFeatures([[e.point.x - 4, e.point.y - 4], [e.point.x + 4, e.point.y + 4]], {layers: drawIds});
  if (!hits.length) { if (popup) popup.remove(); highlight([]); return; }
  const h = hits[0], L = byId[h.sourceLayer], fid = h.properties._fid;
  const a = map.unproject([e.point.x - 4, e.point.y + 4]), b = map.unproject([e.point.x + 4, e.point.y - 4]);
  try { for await (const f of read(L, {minX: a.lng, minY: a.lat, maxX: b.lng, maxY: b.lat})) {
      if (f.properties._fid === fid) { show(L, f, e.lngLat); return; } } } catch (err) { console.warn(err); }
  show(L, {type: "Feature", properties: h.properties, geometry: h.geometry}, e.lngLat);
}
M.on("click", identify);
window.BMS.identifyAt = (lng, lat) => map && identify({point: map.project([lng, lat]), lngLat: {lng: lng, lat: lat}});
// ---- queries
const qL = $("qLayer"), qF = $("qField"), qO = $("qOp");
D.layers.forEach(L => { const o = document.createElement("option"); o.value = L.id; o.textContent = L.name; qL.appendChild(o); });
for (const [k, v] of Object.entries(T.ops)) { const o = document.createElement("option"); o.value = k; o.textContent = v; qO.appendChild(o); }
const fields = () => { qF.textContent = ""; const L = D.layers.find(x => x.id === qL.value); (L ? L.fields : []).forEach(f => { const o = document.createElement("option"); o.value = f; o.textContent = f; qF.appendChild(o); }); };
qL.onchange = fields; fields();
function test(v, op, want) {
  if (v === null || v === undefined) return op === "ne";
  const n = Number(v), w = Number(want), num = want !== "" && !isNaN(w) && !isNaN(n) && typeof v !== "boolean";
  const s = String(v).toLowerCase(), t = String(want).toLowerCase();
  switch (op) { case "eq": return num ? n === w : s === t; case "ne": return num ? n !== w : s !== t; case "has": return s.includes(t);
    case "starts": return s.startsWith(t); case "gt": return num ? n > w : s > t; case "lt": return num ? n < w : s < t; } return false; }
async function query(layerId, field, op, value, inView, limit) {
  const L = D.layers.find(x => x.id === layerId); if (!L) return {found: [], read: 0};
  let rect = null; if (inView && map) { const b = map.getBounds(); rect = {minX: b.getWest(), minY: b.getSouth(), maxX: b.getEast(), maxY: b.getNorth()}; }
  const found = []; let n = 0, total = 0;
  for await (const f of read(L, rect)) { n++; if (test(f.properties[field], op, value)) { total++; if (found.length < (limit || 500)) found.push(f); } }
  return {L: L, found: found, read: n, total: total};
}
window.BMS.query = query;
$("qRun").onclick = async () => {
  const msg = $("qmsg"), ul = $("qres"); ul.textContent = ""; msg.textContent = T.reading;
  try { const r = await query(qL.value, qF.value, qO.value, $("qVal").value, $("qView").checked, 500);
    if (!r.total) { msg.textContent = T.none; highlight([]); return; }
    msg.textContent = fmt(T.found, {n: r.total.toLocaleString(), m: r.read.toLocaleString()}) + (r.total > r.found.length ? " " + fmt(T.first, {n: r.found.length}) : "");
    highlight(r.found);
    r.found.forEach(f => { const li = document.createElement("li"); const p = f.properties;
      li.textContent = String(p[qF.value] ?? "") + " — " + (r.L.fields.filter(k => k !== qF.value).map(k => p[k]).find(v => v !== null && v !== undefined && v !== "") ?? "");
      li.onclick = () => { const b = bboxOf(f.geometry); if (map) map.fitBounds([[b[0], b[1]], [b[2], b[3]]], {padding: 80, maxZoom: 18});
        show(r.L, f, [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2]); }; ul.appendChild(li); });
  } catch (err) { msg.textContent = fmt(T.readfail, {e: err && err.message || err}); }
};
$("qClear").onclick = () => { $("qres").textContent = ""; $("qmsg").textContent = ""; highlight([]); if (popup) popup.remove(); };
// ---- the signature: manifest.json is signed as it is (its exact bytes); each file is checked against its SHA-256
const badge = $("badge"), sigBox = $("sig");
const SIG = {state: "checking", bad: [], checked: 0};
window.BMS.sig = SIG;
function setBadge(cls, text) { badge.className = cls; badge.querySelector("span").textContent = text; }
function hex(b) { return [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, "0")).join(""); }
function sigHtml() { const m = SIG.man; let h = "";
  if (SIG.state === "unsigned") return esc(T.unsigned);
  if (SIG.state === "nocheck") h += `<p>${esc(T.nocheck)}</p>`;
  if (SIG.state === "badsig") h += `<p style="color:#b91c1c;font-weight:600">${esc(T.badsig)}</p>`;
  if (SIG.state === "otherkey") h += `<p style="color:#b91c1c;font-weight:600">${esc(T.otherkey)}</p>`;
  if (m) h += `<div><b>${esc(fmt(T.signed, {name: m.signer.name || "?"}))}</b></div><div>${esc(fmt(T.key, {fp: ""}))}<code>${esc(m.signer.fp)}</code></div><div style="color:#5b6b76">${esc(m.created)}</div>`;
  if (SIG.bad.length) h += `<p style="color:#b91c1c;font-weight:600">${esc(fmt(T.altered, {files: ""}))}</p><ul>${SIG.bad.map(f => "<li><code>" + esc(f) + "</code></li>").join("")}</ul>`;
  h += `<div style="margin-top:6px">${esc(fmt(T.checked, {n: SIG.checked}))}</div>`;
  if (m && SIG.subtle) h += `<div class="row"><button class="btn" type="button" id="sigAll">${esc(T.checkall)}</button></div>`;
  return h; }
function refresh() { if (!sigBox.classList.contains("hide")) { sigBox.innerHTML = sigHtml(); const b = $("sigAll"); if (b) b.onclick = () => checkFiles(Infinity); } }
badge.onclick = () => { sigBox.classList.toggle("hide"); refresh(); };
async function checkFiles(maxSize) {
  const files = SIG.man.files; const todo = Object.keys(files).filter(k => files[k].size <= maxSize && !SIG.done[k]);
  for (const k of todo) { try { const r = await fetch(base + k, {cache: "no-store"}); const b = await r.arrayBuffer();
      const ok = r.ok && b.byteLength === files[k].size && hex(await crypto.subtle.digest("SHA-256", b)) === files[k].sha256;
      SIG.done[k] = true; SIG.checked++; if (!ok) SIG.bad.push(k); } catch (e) { SIG.bad.push(k); } }
  paint(); refresh(); SIG.filesDone = true; }
function paint() { const m = SIG.man;
  if (SIG.state === "unsigned") return setBadge("warn", T.unsigned);
  if (SIG.state === "badsig" || SIG.state === "otherkey" || SIG.bad.length) return setBadge("bad", SIG.bad.length ? fmt(T.altered, {files: SIG.bad.length}) : (SIG.state === "otherkey" ? T.otherkey : T.badsig));
  if (SIG.state === "nocheck") return setBadge("warn", fmt(T.signed, {name: m.signer.name || "?"}) + " ?");
  setBadge("ok", fmt(T.signed, {name: m.signer.name || "?"})); }
(async () => {
  setBadge("", T.checking);
  let body, sigTxt;
  try { const a = await fetch(base + "manifest.json", {cache: "no-store"}), b = await fetch(base + "manifest.sig", {cache: "no-store"});
    if (!a.ok || !b.ok) throw new Error("missing"); body = await a.arrayBuffer(); sigTxt = (await b.text()).trim(); }
  catch (e) { SIG.state = "unsigned"; paint(); return; }
  SIG.man = JSON.parse(new TextDecoder().decode(body)); SIG.done = {};
  SIG.subtle = !!(window.crypto && crypto.subtle);
  const want = new URLSearchParams(location.hash.slice(1)).get("fp");
  if (want && !SIG.man.signer.pub.toLowerCase().startsWith(want.toLowerCase())) { SIG.state = "otherkey"; paint(); return; }
  if (!SIG.subtle) { SIG.state = "nocheck"; paint(); return; }
  let ok = null;
  try { const pub = Uint8Array.from(SIG.man.signer.pub.match(/../g).map(h => parseInt(h, 16)));
    const sig = Uint8Array.from(atob(sigTxt), c => c.charCodeAt(0));
    const key = await crypto.subtle.importKey("raw", pub, {name: "Ed25519"}, false, ["verify"]);
    ok = await crypto.subtle.verify({name: "Ed25519"}, key, sig, body); } catch (e) { ok = null; }
  SIG.state = ok === null ? "nocheck" : (ok ? "ok" : "badsig"); paint();
  if (ok !== false) await checkFiles(5 * 1024 * 1024);
})();
</script></body></html>
"""
