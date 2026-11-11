"""The map service folder: written here, checked here (SPEC.md).

Layout of a published folder (every path is relative, so the folder can move to any host)::

    index.html            the viewer (map, identify, queries, the signature check)
    service.json          what the service holds (layers, fields, files, bounds) - for programs
    manifest.json         every file with its SHA-256 and size, the signer's public key   (BMS1)
    manifest.sig          ed25519 signature of the exact bytes of manifest.json
    data/map.pmtiles      ONE archive: the vector tiles of every layer (source-layer = the layer id)
    data/<id>.fgb         one FlatGeobuf per layer, with its spatial index: identify and queries
    data/raster.pmtiles   optional: one raster as PNG tiles
    ogc/                  a static OGC API - Features tree (ogc/index.json is the landing page)
    xyz/{z}/{x}/{y}.pbf   optional: the same vector tiles as plain files, described by tiles.json
    lib/                  MapLibre, pmtiles.js, flatgeobuf.js, the Arabic font (no CDN)
"""
import datetime
import getpass
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import urllib.parse
import urllib.request

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject,
                       QgsRasterLayer, QgsRectangle, QgsVectorLayer, QgsWkbTypes)

from .. import i18n
from ..i18n import tr
from ..secure import crypto
from ..version import APP_NAME

FORMAT = "BMS1"
MANIFEST = "manifest.json"
SIGNATURE = "manifest.sig"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
OWN_ASSETS = (("flatgeobuf-geojson.min.js", "lib/flatgeobuf.js"),
              ("LICENSE-flatgeobuf.txt", "lib/LICENSE-flatgeobuf.txt"))
FLATGEOBUF_VERSION = "4.4.0"     # web/flatgeobuf-geojson.min.js (BSD-3-Clause)
DEFAULTS = {
    "description": "",
    "min_zoom": 0,
    "max_zoom": 14,
    "ogc": True,               # the static OGC API - Features tree
    "page_size": 1000,         # features per static OGC page
    "xyz": False,              # also {z}/{x}/{y}.pbf files + tiles.json (other GIS programs)
    "sign": True,
    "signer": "",              # the name shown with the signature (default: the Windows user)
    "hide": None,              # {layer name or id: [field names]} never published
    "raster": None,            # one QgsRasterLayer, tiled into data/raster.pmtiles
    "raster_max_zoom": None,   # default: from the raster's resolution (at most 19)
    "basemap": "osm",          # "osm" (online tiles) | "none"
    "basemap_pack": None,      # a PMTiles basemap: the area of the data is cut into the folder (offline)
    "base_url": "",            # the final address, when known: OGC links become absolute
}
_TEST_SEED = []                # tests: a fixed key instead of this PC's dashboard key


class MapServiceError(Exception):
    pass


# ---- small helpers --------------------------------------------------------------------------------------------
def _gdal():
    from osgeo import gdal

    return gdal


def _slug(name, used, i):
    """An address-safe id for a layer: ASCII letters and digits, else layer<n>."""
    s = "".join(ch.lower() if ch.isascii() and ch.isalnum() else "-" for ch in name or "")
    s = "-".join(p for p in s.split("-") if p)[:40] or f"layer{i + 1}"
    cand, k = s, 2
    while cand in used:
        cand, k = f"{s}-{k}", k + 1
    used.add(cand)
    return cand


def _seed():
    if _TEST_SEED:
        return _TEST_SEED[0]
    from ..dashboard import cards

    return cards.seed()


def public_hex():
    return crypto.public_key(_seed()).hex()


def _dump(path, obj, compact=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)


def _translate(dst, src, what, **kw):
    gdal = _gdal()
    with gdal.ExceptionMgr(useExceptions=True):
        try:
            ds = gdal.VectorTranslate(dst, src, options=gdal.VectorTranslateOptions(**kw))
        except RuntimeError as e:
            raise MapServiceError(f"{what}: {e}") from e
    if ds is None:
        raise MapServiceError(f"{what}: {gdal.GetLastErrorMsg()}")
    ds = None  # noqa: F841 - flush to the disk


def _hidden_for(layer, hide):
    if not hide:
        return set()
    out = set()
    for key in (layer.name(), layer.id()):
        out.update(hide.get(key, ()) or ())
    return {str(h) for h in out}


# ---- one layer: what the map draws and what the queries read ---------------------------------------------------
def _collect(layer, project, hide):
    """The features of a layer in WGS 84 as drawn by its style.
    -> dict(kind, fields, styles, legend, tiles: [GeoJSON features for the tiles], full: [... with the fields],
            bounds: QgsRectangle)"""
    from ..share.styles import ExpressionEvaluator, LayerStyler, geometry_kind, label_expression
    from ..share.webmap import MAX_LABELS, json_value, popup_fields

    kind = geometry_kind(layer)
    xform = QgsCoordinateTransform(layer.crs(), WGS84, project.transformContext())
    hidden = _hidden_for(layer, hide)
    fields = [(i, n) for i, n in popup_fields(layer) if n not in hidden and layer.fields()[i].name() not in hidden]
    label_of = ExpressionEvaluator(layer, label_expression(layer))
    tiles, full, n_labels = [], [], 0
    bounds = QgsRectangle()
    bounds.setNull()
    with LayerStyler(layer) as styler:
        for f in layer.getFeatures():
            g = f.geometry()
            if g is None or g.isNull() or g.isEmpty():
                continue
            s = styler.style_index(f)
            if s is None:            # the style does not draw it: it is not published either
                continue
            g = QgsGeometry(g)
            if QgsWkbTypes.isCurvedType(g.wkbType()):
                g = QgsGeometry(g.constGet().segmentize())
            try:
                g.transform(xform)
            except Exception:  # noqa: BLE001 - a feature that cannot be projected is left out
                continue
            if g.isEmpty():
                continue
            bounds.combineExtentWith(g.boundingBox())
            gj = json.loads(g.asJson(7))
            fid = int(f.id())
            props = {}
            for i, name in fields:
                props[name] = json_value(f.attribute(i))
            full.append({"type": "Feature", "id": fid, "properties": props, "geometry": gj})
            tp = {"_s": s, "_fid": fid}
            if label_of.expr is not None and n_labels < MAX_LABELS:
                lbl = label_of(f)
                if lbl:
                    tp["_l"] = str(lbl)
                    n_labels += 1
            tiles.append({"type": "Feature", "properties": tp, "geometry": gj})
        legend = styler.legend()
        styles = styler.styles or [{}]
    return {"kind": kind, "fields": [n for _, n in fields], "styles": styles, "legend": legend,
            "tiles": tiles, "full": full, "bounds": bounds}


# ---- the raster -------------------------------------------------------------------------------------------------
def _raster(layer, path, tmp, max_zoom=None):
    """One raster layer -> PNG tiles in a PMTiles archive. -> dict(min_zoom, max_zoom, bounds) or None."""
    from ..share import pmtiles as pm

    gdal = _gdal()
    src = layer.source().split("|")[0]
    warped = os.path.join(tmp, "raster3857.tif")
    with gdal.ExceptionMgr(useExceptions=True):
        try:
            ds = gdal.Warp(warped, src, options=gdal.WarpOptions(dstSRS="EPSG:3857", dstAlpha=True,
                                                                resampleAlg="bilinear", format="GTiff"))
            gt, w, h = ds.GetGeoTransform(), ds.RasterXSize, ds.RasterYSize
            ds = None
            res = abs(gt[1])
            z_native = max(0, min(19, int(math.ceil(math.log2(156543.03392804097 / max(res, 1e-6))))))
            zmax = min(z_native, int(max_zoom)) if max_zoom is not None else z_native
            mb = os.path.join(tmp, "raster.mbtiles")
            ds = gdal.Translate(mb, warped, options=gdal.TranslateOptions(
                format="MBTILES", creationOptions=["TILE_FORMAT=PNG", "RESAMPLING=BILINEAR",
                                                   "ZOOM_LEVEL_STRATEGY=UPPER" if zmax >= z_native else "LOWER"]))
            levels, k = [], 2
            while max(w, h) / k >= 32 and len(levels) < 18:
                levels.append(k)
                k *= 2
            if levels:
                ds.BuildOverviews("AVERAGE", levels)
            ds = None
        except RuntimeError as e:
            raise MapServiceError(f"raster: {e}") from e
    tiles = {}
    con = sqlite3.connect(mb)
    try:
        for z, x, row, data in con.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"):
            if z <= zmax:
                tiles[(z, x, (1 << z) - 1 - row)] = bytes(data)
    finally:
        con.close()
    if not tiles:
        return None
    ext = layer.extent()
    xf = QgsCoordinateTransform(layer.crs(), WGS84, QgsProject.instance().transformContext())
    try:
        b = xf.transformBoundingBox(ext)
        bounds = (b.xMinimum(), b.yMinimum(), b.xMaximum(), b.yMaximum())
    except Exception:  # noqa: BLE001
        bounds = None
    zs = [k[0] for k in tiles]
    pm.write(path, tiles, tile_type=2, tile_compression=1, metadata={"name": layer.name(), "type": "overlay"},
             bounds=bounds)
    return {"min_zoom": min(zs), "max_zoom": max(zs), "bounds": list(bounds) if bounds else None}


# ---- the signature -------------------------------------------------------------------------------------------
def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for buf in iter(lambda: f.read(1 << 20), b""):
            h.update(buf)
    return h.hexdigest()


def _sign(out_dir, title, signer):
    """manifest.json (canonical bytes) + manifest.sig. -> (public key hex, fingerprint)"""
    files = {}
    for root, _dirs, names in os.walk(out_dir):
        for n in names:
            p = os.path.join(root, n)
            rel = os.path.relpath(p, out_dir).replace(os.sep, "/")
            if rel in (MANIFEST, SIGNATURE):
                continue
            files[rel] = {"sha256": _sha(p), "size": os.path.getsize(p)}
    seed = _seed()
    pub = crypto.public_key(seed)
    man = {"v": 1, "format": FORMAT, "title": title,
           "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "signer": {"name": signer, "pub": pub.hex(), "fp": crypto.fingerprint(pub)}, "files": files}
    body = crypto.canonical(man)
    with open(os.path.join(out_dir, MANIFEST), "wb") as f:
        f.write(body)
    with open(os.path.join(out_dir, SIGNATURE), "w", encoding="ascii", newline="\n") as f:
        f.write(crypto.b64(crypto.sign(seed, body)))
    return pub.hex(), crypto.fingerprint(pub)


def _read(where, rel):
    if where.startswith(("http://", "https://")):
        url = urllib.parse.urljoin(where if where.endswith("/") else where + "/", rel)
        req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - the user's own address
            return r.read()
    with open(os.path.join(where, *rel.split("/")), "rb") as f:
        return f.read()


def verify(where, expected_pub=None, files=True):
    """Check a published service (a folder or an http(s) address).
    -> dict(ok, signed, signature_ok, signer, pub, fingerprint, by_me, bad: [paths], missing: [paths], checked)"""
    out = {"ok": False, "signed": False, "signature_ok": False, "signer": "", "pub": "", "fingerprint": "",
           "by_me": False, "bad": [], "missing": [], "checked": 0}
    try:
        body = _read(where, MANIFEST)
        sig = crypto.unb64(_read(where, SIGNATURE).decode("ascii").strip())
    except (OSError, ValueError):
        return out
    out["signed"] = True
    try:
        man = json.loads(body.decode("utf-8"))
        pub = bytes.fromhex(man["signer"]["pub"])
    except (ValueError, KeyError, TypeError):
        return out
    out.update(signer=man["signer"].get("name", ""), pub=pub.hex(), fingerprint=crypto.fingerprint(pub))
    out["signature_ok"] = crypto.verify(pub, sig, body) and (expected_pub is None or expected_pub == pub.hex())
    try:
        out["by_me"] = pub.hex() == public_hex()
    except Exception:  # noqa: BLE001 - no key on this PC yet
        out["by_me"] = False
    if files:
        for rel, meta in sorted((man.get("files") or {}).items()):
            try:
                data = _read(where, rel)
            except OSError:
                out["missing"].append(rel)
                continue
            out["checked"] += 1
            if hashlib.sha256(data).hexdigest() != meta.get("sha256") or len(data) != meta.get("size"):
                out["bad"].append(rel)
    out["ok"] = out["signature_ok"] and not out["bad"] and not out["missing"]
    return out


# ---- publish --------------------------------------------------------------------------------------------------
def publish(layers, out_dir, title="", options=None, project=None, progress=None):
    """Write the map service folder.

    layers: vector layers (top of the map first); options: see DEFAULTS. progress: callable(percent, text).
    -> dict(index, folder, layers: [dict(id, name, count, fgb, collection)], raster, files: [(path, bytes)],
            signed, pub, fingerprint, link_suffix, warnings, bounds, ogc, tilejson)"""
    from ..share import offline_maps as om
    from ..share.pages_publish import _cut_basemap, _labels, _paint
    from . import ogc as ogc_mod
    from . import viewer

    o = dict(DEFAULTS)
    o.update(options or {})
    project = project or QgsProject.instance()
    say = progress or (lambda pct, text="": None)
    layers = [lyr for lyr in layers if isinstance(lyr, QgsVectorLayer) and lyr.isValid() and lyr.isSpatial()]
    raster = o.get("raster")
    if raster is not None and not (isinstance(raster, QgsRasterLayer) and raster.isValid()):
        raster = None
    if not layers and raster is None:
        raise MapServiceError(tr("Choose at least one layer."))
    if not 0 <= int(o["min_zoom"]) <= int(o["max_zoom"]) <= 22:
        raise MapServiceError("zoom")
    title = title or project.title() or APP_NAME
    signer = o.get("signer") or project.metadata().author() or getpass.getuser()
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    for old in ("data", "ogc", "xyz", "lib"):        # a new publish replaces what the old one wrote
        shutil.rmtree(os.path.join(out_dir, old), ignore_errors=True)
    for old in (MANIFEST, SIGNATURE, "tiles.json", "service.json"):
        if os.path.exists(os.path.join(out_dir, old)):
            os.remove(os.path.join(out_dir, old))
    data_dir = os.path.join(out_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    warnings, entries, used = [], [], set()
    bounds = QgsRectangle()
    bounds.setNull()
    tmp = tempfile.mkdtemp(prefix="biruni_mapservice_")
    try:
        gpkg = os.path.join(tmp, "tiles.gpkg")
        n = max(1, len(layers))
        for i, lyr in enumerate(layers):
            say(int(5 + 45 * i / n), lyr.name())
            d = _collect(lyr, project, o.get("hide"))
            if not d["full"]:
                warnings.append(tr("{name}: nothing to publish (no features drawn).").format(name=lyr.name()))
                continue
            lid = _slug(lyr.name(), used, i)
            bounds.combineExtentWith(d["bounds"])
            tj = os.path.join(tmp, lid + "_tiles.geojson")
            _dump(tj, {"type": "FeatureCollection", "features": d["tiles"]})
            _translate(gpkg, tj, "tiles " + lyr.name(), format="GPKG", layerName=lid,
                       accessMode="update" if os.path.exists(gpkg) else None)
            fj = os.path.join(tmp, lid + "_full.geojson")
            _dump(fj, {"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": dict({"_fid": f["id"]}, **f["properties"]), "geometry": f["geometry"]}
                for f in d["full"]]})
            _translate(os.path.join(data_dir, lid + ".fgb"), fj, "FlatGeobuf " + lyr.name(), format="FlatGeobuf",
                       layerName=lid, geometryType="PROMOTE_TO_MULTI", layerCreationOptions=["SPATIAL_INDEX=YES"])
            b = d["bounds"]
            entries.append({"id": lid, "name": lyr.name(), "kind": d["kind"], "fields": d["fields"],
                            "count": len(d["full"]), "fgb": f"data/{lid}.fgb",
                            "bbox": [round(b.xMinimum(), 7), round(b.yMinimum(), 7), round(b.xMaximum(), 7),
                                     round(b.yMaximum(), 7)],
                            "layers": _paint(d["kind"], d["styles"], lid) + [_labels(d["kind"], lid)],
                            "legend": d["legend"], "_full": d["full"]})
        tiles_info = None
        if entries:
            say(55, tr("Vector tiles"))
            co = [f"MINZOOM={int(o['min_zoom'])}", f"MAXZOOM={int(o['max_zoom'])}", "NAME=" + title[:60]]
            _translate(os.path.join(data_dir, "map.pmtiles"), gpkg, "PMTiles", format="PMTiles",
                       datasetCreationOptions=co)
            tiles_info = {"file": "data/map.pmtiles", "min_zoom": int(o["min_zoom"]), "max_zoom": int(o["max_zoom"])}
            if o.get("xyz"):
                say(62, tr("Tiles as files"))
                _translate(os.path.join(out_dir, "xyz"), gpkg, "MVT", format="MVT", datasetCreationOptions=co + [
                    "COMPRESS=NO", "FORMAT=DIRECTORY", "TILE_EXTENSION=pbf"])
        rinfo = None
        if raster is not None:
            say(66, raster.name())
            rinfo = _raster(raster, os.path.join(data_dir, "raster.pmtiles"), tmp, o.get("raster_max_zoom"))
            if rinfo is None:
                warnings.append(tr("{name}: the raster gave no tiles.").format(name=raster.name()))
            else:
                rinfo.update(file="data/raster.pmtiles", name=raster.name())
                if rinfo["bounds"]:
                    r = rinfo["bounds"]
                    bounds.combineExtentWith(QgsRectangle(r[0], r[1], r[2], r[3]))
        if not entries and rinfo is None:
            raise MapServiceError(tr("Nothing to publish."))
        bb = None if bounds.isNull() else [round(bounds.xMinimum(), 7), round(bounds.yMinimum(), 7),
                                           round(bounds.xMaximum(), 7), round(bounds.yMaximum(), 7)]
        base = None
        if o.get("basemap_pack") and bb:
            say(70, tr("Basemap"))
            if _cut_basemap(o["basemap_pack"], os.path.join(data_dir, "basemap.pmtiles"), bb) is not None:
                base = {"file": "data/basemap.pmtiles", "layers": om.web_basemap_layers(),
                        "attribution": om.OSM_ATTRIBUTION}
        ogc_url = tilejson = None
        if o.get("ogc") and entries:
            say(75, "OGC API - Features")
            ogc_mod.write(os.path.join(out_dir, "ogc"), title, o.get("description") or "",
                          [{"id": e["id"], "title": e["name"], "bbox": e["bbox"], "features": e["_full"],
                            "fields": e["fields"]} for e in entries], int(o.get("page_size") or 1000),
                          o.get("base_url") or "")
            ogc_url = "ogc/"
        if o.get("xyz") and entries:
            tilejson = "tiles.json"
            base_url = (o.get("base_url") or "").rstrip("/")
            _dump(os.path.join(out_dir, tilejson), {
                "tilejson": "3.0.0", "name": title, "description": o.get("description") or "",
                "attribution": "", "scheme": "xyz", "minzoom": int(o["min_zoom"]), "maxzoom": int(o["max_zoom"]),
                "bounds": bb or [-180, -85.0511, 180, 85.0511],
                "tiles": [(base_url + "/" if base_url else "") + "xyz/{z}/{x}/{y}.pbf"],
                "vector_layers": [{"id": e["id"], "description": e["name"],
                                   "fields": {"_fid": "Number", "_s": "Number", "_l": "String"}} for e in entries]},
                compact=False)
        for e in entries:
            e.pop("_full", None)
        say(85, tr("Viewer"))
        om.copy_web_assets(out_dir, basemap=base is not None)
        for src, rel in OWN_ASSETS:
            shutil.copyfile(os.path.join(WEB, src), os.path.join(out_dir, *rel.split("/")))
        service = {"format": FORMAT, "title": title, "description": o.get("description") or "",
                   "bounds": bb, "tiles": tiles_info, "raster": rinfo, "basemap": base,
                   "online_basemap": o.get("basemap") == "osm" and base is None,
                   "layers": [{k: v for k, v in e.items() if k not in ("layers", "legend")} for e in entries],
                   "ogc": ogc_url, "tilejson": tilejson, "base_url": (o.get("base_url") or "").rstrip("/"),
                   "generator": APP_NAME}
        _dump(os.path.join(out_dir, "service.json"), service, compact=False)
        viewer.write(os.path.join(out_dir, "index.html"), title, o.get("description") or "", entries, service,
                     rtl=i18n.is_rtl(), lang=("ar" if i18n.is_rtl() else (i18n.lang() or "en")))
        with open(os.path.join(out_dir, ".nojekyll"), "w", encoding="utf-8") as f:
            f.write("")
        with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write(readme(title, ogc_url, tilejson))
        pub = fp = ""
        if o.get("sign", True):
            say(95, tr("Signing"))
            pub, fp = _sign(out_dir, title, signer)
        files = []
        for root, _d, names in os.walk(out_dir):
            for nm in names:
                p = os.path.join(root, nm)
                files.append((os.path.relpath(p, out_dir).replace(os.sep, "/"), os.path.getsize(p)))
        say(100, "")
        return {"index": os.path.join(out_dir, "index.html"), "folder": out_dir,
                "layers": [{"id": e["id"], "name": e["name"], "count": e["count"], "fgb": e["fgb"],
                            "collection": f"ogc/collections/{e['id']}/" if ogc_url else None} for e in entries],
                "raster": rinfo, "files": sorted(files), "signed": bool(pub), "pub": pub, "fingerprint": fp,
                "link_suffix": ("#fp=" + pub[:16]) if pub else "", "warnings": warnings, "bounds": bb,
                "ogc": ogc_url, "tilejson": tilejson}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def rebase(out_dir, base_url, sign=True):
    """Point the OGC links (and the TileJSON tiles) of a published folder at a new address - the host it was
    copied to, or this PC's preview server - and sign the folder again. GIS programs need absolute links.
    -> the number of files rewritten"""
    base_url = (base_url or "").rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise MapServiceError("the address must start with http:// or https://")
    svc_path = os.path.join(out_dir, "service.json")
    with open(svc_path, encoding="utf-8") as f:
        svc = json.load(f)
    old = (svc.get("base_url") or "").rstrip("/")
    ogc_dir = os.path.join(out_dir, "ogc")
    new_root = base_url + "/ogc/"

    def fix(o, doc_dir):
        if isinstance(o, dict):
            for k, v in list(o.items()):
                if k == "features":          # the data itself is never touched
                    continue
                if k == "href" and isinstance(v, str):
                    if old and v.startswith(old + "/ogc/"):
                        o[k] = new_root + v[len(old) + 5:]
                    elif not v.startswith(("http://", "https://")):
                        o[k] = urllib.parse.urljoin(new_root + (doc_dir + "/" if doc_dir else ""), v)
                else:
                    fix(v, doc_dir)
        elif isinstance(o, list):
            for x in o:
                fix(x, doc_dir)

    n = 0
    if os.path.isdir(ogc_dir):
        for root, _d, names in os.walk(ogc_dir):
            for nm in names:
                if nm == "about.html":
                    continue
                p = os.path.join(root, nm)
                try:
                    with open(p, encoding="utf-8") as f:
                        obj = json.load(f)
                except ValueError:
                    continue
                doc_dir = os.path.relpath(root, ogc_dir).replace(os.sep, "/").strip(".")
                if nm == "index.html" and doc_dir == "api":
                    obj["servers"] = [{"url": new_root.rstrip("/")}]
                else:
                    fix(obj, doc_dir)
                with open(p, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
                n += 1
    tj = os.path.join(out_dir, "tiles.json")
    if os.path.exists(tj):
        with open(tj, encoding="utf-8") as f:
            t = json.load(f)
        t["tiles"] = [base_url + "/xyz/{z}/{x}/{y}.pbf"]
        _dump(tj, t, compact=False)
        n += 1
    svc["base_url"] = base_url
    _dump(svc_path, svc, compact=False)
    if sign and os.path.exists(os.path.join(out_dir, MANIFEST)):
        with open(os.path.join(out_dir, MANIFEST), encoding="utf-8") as f:
            man = json.load(f)
        _sign(out_dir, man.get("title") or svc.get("title") or "", (man.get("signer") or {}).get("name", ""))
    return n


def readme(title, ogc_url, tilejson):
    lines = [f"# {title}", "", f"A map service published with {APP_NAME}: it needs no server of its own.", "",
             "Put this folder on any web host that answers HTTP range requests (GitHub Pages, Netlify, a web server "
             "on the office network) and open index.html from its address.", "",
             "* `data/map.pmtiles`: the map (vector tiles of every layer, one file).",
             "* `data/*.fgb`: one FlatGeobuf per layer, read in pieces for identify and queries."]
    if ogc_url:
        lines.append(f"* `{ogc_url}`: OGC API - Features (static): add this address in a GIS program.")
    if tilejson:
        lines.append(f"* `{tilejson}`: TileJSON of the same tiles as plain `xyz/{{z}}/{{x}}/{{y}}.pbf` files.")
    lines += ["* `manifest.json` + `manifest.sig`: every file with its SHA-256, signed (ed25519). The viewer checks "
              "them; Biruni checks them with Check a map service.", ""]
    return "\n".join(lines)


def file_size_text(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)
