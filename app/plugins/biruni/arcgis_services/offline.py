"""Take a service layer offline, edit it anywhere, then sync the edits back.

    take_offline()  copies a feature service layer into a GeoPackage: the features, the server's object id of
                    each one, and a snapshot of what was downloaded (attributes, geometry, last-edited date)
    pending()       what changed in the GeoPackage since: added, changed and deleted features (no network)
    sync()          sends the edits with applyEdits, after checking each changed / deleted feature against the
                    server: a feature that someone else changed in the meantime (editor tracking date, or its
                    attributes / geometry when the layer has no editor tracking) is a conflict and is not sent
                    unless the user chose to keep their version ("mine") or the server's ("theirs"). Changes made
                    on the server to features not edited here are brought into the GeoPackage.

Only the GeoPackage and the REST API are used (requests through rest.Client), so it works the same with
ArcGIS Server, Portal and ArcGIS Online, and it can be tested against a local stand-in server."""
import datetime as _dt
import hashlib
import json
import os
from dataclasses import dataclass, field

from osgeo import gdal, ogr, osr

from .rest import RestError, capabilities, edit_rights, oid_field

META = "biruni_sync_layers"
BASE = "biruni_sync_base"
SKIP, MINE, THEIRS = "skip", "mine", "theirs"

_TYPES = {
    "esriFieldTypeOID": ogr.OFTInteger64, "esriFieldTypeSmallInteger": ogr.OFTInteger,
    "esriFieldTypeInteger": ogr.OFTInteger, "esriFieldTypeBigInteger": ogr.OFTInteger64,
    "esriFieldTypeSingle": ogr.OFTReal, "esriFieldTypeDouble": ogr.OFTReal,
    "esriFieldTypeString": ogr.OFTString, "esriFieldTypeGUID": ogr.OFTString, "esriFieldTypeGlobalID": ogr.OFTString,
    "esriFieldTypeXML": ogr.OFTString, "esriFieldTypeDate": ogr.OFTDateTime, "esriFieldTypeDateOnly": ogr.OFTDate,
    "esriFieldTypeTimeOnly": ogr.OFTTime, "esriFieldTypeTimestampOffset": ogr.OFTDateTime,
}
_GEOM = {"esriGeometryPoint": ogr.wkbPoint, "esriGeometryMultipoint": ogr.wkbMultiPoint,
         "esriGeometryPolyline": ogr.wkbMultiLineString, "esriGeometryPolygon": ogr.wkbMultiPolygon}
_SERVER_FIELDS = ("shape__area", "shape__length", "shape_area", "shape_length", "shape.starea()", "shape.stlength()",
                  "st_area(shape)", "st_length(shape)")


class OfflineError(Exception):
    pass


@dataclass
class SyncReport:
    added: int = 0
    changed: int = 0
    deleted: int = 0
    pulled: int = 0                                  # features brought in from the server
    conflicts: list = field(default_factory=list)    # (local fid, server object id, reason)
    errors: list = field(default_factory=list)       # (what, message)
    refused: list = field(default_factory=list)      # edits the service does not allow (add / update / delete)

    def text(self, tr=lambda s: s):
        parts = [tr("{n} added").format(n=self.added), tr("{n} changed").format(n=self.changed),
                 tr("{n} deleted").format(n=self.deleted)]
        if self.pulled:
            parts.append(tr("{n} updated from the server").format(n=self.pulled))
        n = len(self.conflicts)
        parts.append((tr("{n} conflict") if n == 1 else tr("{n} conflicts")).format(n=n))
        if self.errors:
            parts.append(tr("{n} failed").format(n=len(self.errors)))
        if self.refused:
            parts.append(tr("{n} not allowed by the service").format(n=len(self.refused)))
        return ", ".join(parts)


# ---- geometry: Esri JSON <-> OGR ------------------------------------------------------------------------------------
def _area2(ring):
    return sum(x0 * y1 - x1 * y0 for (x0, y0, *_), (x1, y1, *_) in zip(ring, ring[1:] + ring[:1]))


def _inside(pt, ring):
    x, y = pt[0], pt[1]
    c = False
    for (x0, y0, *_), (x1, y1, *_) in zip(ring, ring[1:] + ring[:1]):
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / ((y1 - y0) or 1e-300) + x0:
            c = not c
    return c


def esri_to_geojson(g):
    """Esri JSON geometry -> GeoJSON-like dict (None when empty)."""
    if not g:
        return None
    if "x" in g:
        if g.get("x") is None or g.get("x") == "NaN":
            return None
        c = [g["x"], g["y"]] + ([g["z"]] if g.get("z") is not None else [])
        return {"type": "Point", "coordinates": c}
    if "points" in g:
        return {"type": "MultiPoint", "coordinates": g["points"]} if g["points"] else None
    if "paths" in g:
        return {"type": "MultiLineString", "coordinates": g["paths"]} if g["paths"] else None
    if "rings" in g:
        rings = [r for r in g["rings"] if len(r) >= 4]
        if not rings:
            return None
        outers = [[r] for r in rings if _area2(r) < 0]          # clockwise rings are outer rings
        holes = [r for r in rings if _area2(r) >= 0]
        if not outers:
            return {"type": "MultiPolygon", "coordinates": [[r] for r in rings]}
        for h in holes:
            host = next((o for o in sorted(outers, key=lambda o: abs(_area2(o[0])))
                         if _inside(h[0], o[0])), None)
            if host is None:
                outers.append([h])
            else:
                host.append(h)
        return {"type": "MultiPolygon", "coordinates": outers}
    return None


def ogr_to_esri(geom):
    """OGR geometry -> Esri JSON geometry (outer rings clockwise, holes counter-clockwise)."""
    if geom is None or geom.IsEmpty():
        return None
    gj = json.loads(geom.ExportToJson())
    t, c = gj["type"], gj.get("coordinates")
    if t == "Point":
        out = {"x": c[0], "y": c[1]}
        if len(c) > 2:
            out["z"] = c[2]
        return out
    if t == "MultiPoint":
        return {"points": c}
    if t == "LineString":
        return {"paths": [c]}
    if t == "MultiLineString":
        return {"paths": c}
    polys = [c] if t == "Polygon" else c if t == "MultiPolygon" else None
    if polys is None:
        return None
    rings = []
    for poly in polys:
        for k, r in enumerate(poly):
            cw = _area2(r) < 0
            rings.append(r if cw == (k == 0) else list(reversed(r)))
    return {"rings": rings}


def geom_hash(esri):
    """Order-stable fingerprint of a geometry (coordinates rounded to a millimetre-ish 1e-6 of a unit)."""
    if not esri:
        return ""

    def rnd(v):
        if isinstance(v, list):
            return [rnd(x) for x in v]
        return round(float(v), 6) if isinstance(v, (int, float)) else v
    gj = esri_to_geojson(esri)
    return hashlib.sha1(json.dumps(rnd(gj["coordinates"]) if gj else None).encode()).hexdigest()


# ---- values ---------------------------------------------------------------------------------------------------------
def _ms_to_dt(ms):
    return _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(milliseconds=ms)


def _norm(v):
    """Comparable form of an attribute value (OGR or JSON)."""
    if v is None:
        return None
    if isinstance(v, float):
        return round(v, 9)
    return v


def _read_value(f, idx, ftype):
    if not f.IsFieldSetAndNotNull(idx):
        return None
    if ftype in (ogr.OFTDateTime, ogr.OFTDate):
        y, mo, d, h, mi, s, tz = f.GetFieldAsDateTime(idx)
        dt = _dt.datetime(y, mo, d, h, mi, int(s), int(round((s - int(s)) * 1e6)), tzinfo=_dt.timezone.utc)
        return int(round(dt.timestamp() * 1000))
    if ftype == ogr.OFTTime:
        return f.GetFieldAsString(idx)
    if ftype in (ogr.OFTInteger, ogr.OFTInteger64):
        return f.GetFieldAsInteger64(idx)
    if ftype == ogr.OFTReal:
        return f.GetFieldAsDouble(idx)
    return f.GetFieldAsString(idx)


def _write_value(f, name, ftype, v):
    if v is None:
        f.SetFieldNull(name)
    elif ftype in (ogr.OFTDateTime, ogr.OFTDate) and isinstance(v, (int, float)):
        dt = _ms_to_dt(v)
        f.SetField(name, dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second + dt.microsecond / 1e6, 100)
    else:
        f.SetField(name, v)


# ---- the GeoPackage -------------------------------------------------------------------------------------------------
def _open(path, update=True):
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        ds = ogr.Open(path, 1 if update else 0)
    finally:
        gdal.PopErrorHandler()
    if ds is None:
        raise OfflineError(f"cannot open {path}")
    return ds


def _meta_layer(ds, create=False):
    lyr = ds.GetLayerByName(META)
    if lyr is None and create:
        lyr = ds.CreateLayer(META, geom_type=ogr.wkbNone)
        for n in ("layer", "url", "connection", "oid_field", "fields", "edit_fields", "geometry_type", "where"):
            lyr.CreateField(ogr.FieldDefn(n, ogr.OFTString))
        lyr.CreateField(ogr.FieldDefn("wkid", ogr.OFTInteger))
        lyr.CreateField(ogr.FieldDefn("downloaded", ogr.OFTInteger64))
    return lyr


def _base_layer(ds, create=False):
    lyr = ds.GetLayerByName(BASE)
    if lyr is None and create:
        lyr = ds.CreateLayer(BASE, geom_type=ogr.wkbNone)
        for n, t in (("layer", ogr.OFTString), ("oid", ogr.OFTInteger64), ("local", ogr.OFTString),
                     ("server", ogr.OFTString), ("local_geom", ogr.OFTString), ("server_geom", ogr.OFTString),
                     ("edit_date", ogr.OFTInteger64)):
            lyr.CreateField(ogr.FieldDefn(n, t))
    return lyr


def offline_layers(path):
    """Layers of a GeoPackage that were taken offline -> {table name: metadata dict}."""
    try:
        ds = _open(path, update=False)
    except OfflineError:
        return {}
    meta = _meta_layer(ds)
    out = {}
    if meta is not None:
        for f in meta:
            out[f["layer"]] = _meta_dict(f)
    return out


def _meta_dict(f):
    return {"layer": f["layer"], "url": f["url"], "connection": f["connection"], "oid_field": f["oid_field"],
            "fields": json.loads(f["fields"] or "[]"), "edit_fields": json.loads(f["edit_fields"] or "{}"),
            "geometry_type": f["geometry_type"], "wkid": f["wkid"], "downloaded": f["downloaded"],
            "where": f["where"] or "1=1"}


def _server_wkid(info):
    sr = (info.get("extent") or {}).get("spatialReference") or info.get("sourceSpatialReference") or {}
    return sr.get("latestWkid") or sr.get("wkid")


def _srs(wkid):
    s = osr.SpatialReference()
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    if wkid:
        code = {102100: 3857, 102113: 3857}.get(int(wkid), int(wkid))
        if s.ImportFromEPSG(code) != 0:
            return None
        return s
    return None


def take_offline(client, layer_url, path, table=None, where="1=1", progress=None):
    """Copy a feature service layer into the GeoPackage at path (created if needed) -> table name.
    The layer's own coordinate system is kept so the edits go back unchanged."""
    layer_url = layer_url.rstrip("/")
    info = client.info(layer_url)
    caps = capabilities(info)
    if "query" not in caps and caps:
        raise OfflineError("This layer cannot be queried, so it cannot be taken offline.")
    oid = oid_field(info)
    if not oid:
        raise OfflineError("This layer has no object id field.")
    wkid = _server_wkid(info)
    feats = client.query_all(layer_url, where=where, out_sr=wkid, info=info, progress=progress)
    table = table or _safe(info.get("name") or "layer")
    gtype = _GEOM.get(info.get("geometryType"), ogr.wkbNone)
    exists = os.path.exists(path)
    drv = ogr.GetDriverByName("GPKG")
    ds = _open(path) if exists else drv.CreateDataSource(path)
    if ds.GetLayerByName(table) is not None:
        ds.DeleteLayer(table)
    lyr = ds.CreateLayer(table, srs=_srs(wkid), geom_type=gtype, options=["FID=fid", "GEOMETRY_NAME=geom"])
    fields = []
    for fd in info.get("fields") or []:
        t = _TYPES.get(fd.get("type"))
        if t is None or fd.get("name", "").lower() in ("fid", "geom"):
            continue
        d = ogr.FieldDefn(fd["name"], t)
        if t == ogr.OFTString and fd.get("length") and fd["length"] < 100000:
            d.SetWidth(int(fd["length"]))
        if fd.get("alias") and fd["alias"] != fd["name"]:
            d.SetAlternativeName(fd["alias"])
        lyr.CreateField(d)
        editable = fd.get("editable", True) is not False and fd.get("type") not in (
            "esriFieldTypeOID", "esriFieldTypeGlobalID") and fd["name"].lower() not in _SERVER_FIELDS
        fields.append({"name": fd["name"], "type": fd["type"], "ogr": t, "editable": editable})
    ef = dict(info.get("editFieldsInfo") or {})
    if info.get("globalIdField"):
        ef["globalIdField"] = info["globalIdField"]
    defn = lyr.GetLayerDefn()
    lyr.StartTransaction()
    for feat in feats:
        f = ogr.Feature(defn)
        attrs = feat.get("attributes") or {}
        for fd in fields:
            _write_value(f, fd["name"], fd["ogr"], attrs.get(fd["name"]))
        gj = esri_to_geojson(feat.get("geometry"))
        if gj is not None and gtype != ogr.wkbNone:
            g = ogr.CreateGeometryFromJson(json.dumps(gj))
            if g is not None:
                f.SetGeometry(ogr.ForceTo(g, gtype) if g.GetGeometryType() != gtype else g)
        lyr.CreateFeature(f)
    lyr.CommitTransaction()
    meta = _meta_layer(ds, create=True)
    for f in list(meta):
        if f["layer"] == table:
            meta.DeleteFeature(f.GetFID())
    m = ogr.Feature(meta.GetLayerDefn())
    for k, v in (("layer", table), ("url", layer_url), ("connection", client.conn.name), ("oid_field", oid),
                 ("fields", json.dumps([{k2: v2 for k2, v2 in x.items() if k2 != "ogr"} for x in fields])),
                 ("edit_fields", json.dumps(ef)), ("geometry_type", info.get("geometryType") or ""),
                 ("where", where)):
        m.SetField(k, v)
    if wkid:
        m.SetField("wkid", int(wkid))
    m.SetField("downloaded", _now_ms())
    meta.CreateFeature(m)
    _clear_base(ds, table)
    ds = None
    # the snapshot is read back from the GeoPackage so later comparisons see the same values and geometries
    server = {feat["attributes"].get(oid): feat for feat in feats if (feat.get("attributes") or {}).get(oid) is not None}
    _snapshot(path, table, server)
    return table


def _safe(name):
    s = "".join(c if c.isalnum() or c == "_" else "_" for c in name).strip("_") or "layer"
    return s if not s[0].isdigit() else "l_" + s


def _now_ms():
    import time
    return int(time.time() * 1000)


def _clear_base(ds, table, oids=None):
    base = _base_layer(ds, create=True)
    base.SetAttributeFilter(f"layer = '{table}'")
    gone = [f.GetFID() for f in base if oids is None or f["oid"] in oids]
    base.SetAttributeFilter(None)
    for fid in gone:
        base.DeleteFeature(fid)


def _local_rows(ds, table, meta):
    """{fid: (server oid or None, {field: value}, esri geometry)} of the GeoPackage layer."""
    lyr = ds.GetLayerByName(table)
    if lyr is None:
        raise OfflineError(f"the layer {table} is not in this GeoPackage")
    defn = lyr.GetLayerDefn()
    idx = {defn.GetFieldDefn(i).GetName(): (i, defn.GetFieldDefn(i).GetType()) for i in range(defn.GetFieldCount())}
    oid = meta["oid_field"]
    out = {}
    lyr.ResetReading()
    for f in lyr:
        vals = {n: _read_value(f, i, t) for n, (i, t) in idx.items()}
        out[f.GetFID()] = (vals.get(oid), vals, ogr_to_esri(f.GetGeometryRef()))
    return out, idx


def _editable_values(meta, vals):
    return {fd["name"]: _norm(vals.get(fd["name"])) for fd in meta["fields"] if fd["editable"]}


def _snapshot(path, table, server, only=None):
    """Write the base snapshot of (some) features: what the GeoPackage holds now and what the server sent."""
    ds = _open(path)
    meta = _meta_dict(next(f for f in _meta_layer(ds) if f["layer"] == table))
    rows, _idx = _local_rows(ds, table, meta)
    edit_date = (meta["edit_fields"] or {}).get("editDateField")
    base = _base_layer(ds, create=True)
    if only is not None:
        _clear_base(ds, table, set(only))
    base.StartTransaction()
    for fid, (oid, vals, geom) in rows.items():
        if oid is None or (only is not None and oid not in only) or oid not in server:
            continue
        sf = server[oid]
        sattrs = sf.get("attributes") or {}
        b = ogr.Feature(base.GetLayerDefn())
        b.SetField("layer", table)
        b.SetField("oid", int(oid))
        b.SetField("local", json.dumps(_editable_values(meta, vals)))
        b.SetField("server", json.dumps(_editable_values(meta, sattrs)))
        b.SetField("local_geom", geom_hash(geom))
        b.SetField("server_geom", geom_hash(sf.get("geometry")))
        if edit_date and sattrs.get(edit_date) is not None:
            b.SetField("edit_date", int(sattrs[edit_date]))
        base.CreateFeature(b)
    base.CommitTransaction()
    ds = None


def _base(ds, table):
    base = _base_layer(ds, create=False)
    out = {}
    if base is None:
        return out
    base.SetAttributeFilter(f"layer = '{table}'")
    for f in base:
        out[f["oid"]] = {"local": json.loads(f["local"] or "{}"), "server": json.loads(f["server"] or "{}"),
                         "local_geom": f["local_geom"] or "", "server_geom": f["server_geom"] or "",
                         "edit_date": f["edit_date"] if f.IsFieldSetAndNotNull("edit_date") else None}
    base.SetAttributeFilter(None)
    return out


@dataclass
class Pending:
    adds: list = field(default_factory=list)       # local fids
    updates: list = field(default_factory=list)    # (local fid, server oid)
    deletes: list = field(default_factory=list)    # server oids

    @property
    def count(self):
        return len(self.adds) + len(self.updates) + len(self.deletes)


def pending(path, table):
    """What was added, changed and deleted in the GeoPackage since it was taken offline (or last synced)."""
    ds = _open(path, update=False)
    meta = offline_layers(path).get(table)
    if meta is None:
        raise OfflineError(f"{table} was not taken offline from a service")
    rows, _idx = _local_rows(ds, table, meta)
    base = _base(ds, table)
    p = Pending()
    seen = set()
    for fid, (oid, vals, geom) in sorted(rows.items()):
        if oid is None or oid not in base:
            p.adds.append(fid)
            continue
        seen.add(oid)
        b = base[oid]
        if _editable_values(meta, vals) != {k: _norm(v) for k, v in b["local"].items()} or \
                geom_hash(geom) != b["local_geom"]:
            p.updates.append((fid, oid))
    p.deletes = sorted(set(base) - seen)
    return p


# ---- sync -----------------------------------------------------------------------------------------------------------
def sync(client, path, table, policy=SKIP, pull=True, progress=None):
    """Send the GeoPackage's edits to the service and bring the server's changes in -> SyncReport."""
    meta = offline_layers(path).get(table)
    if meta is None:
        raise OfflineError(f"{table} was not taken offline from a service")
    url, oid = meta["url"], meta["oid_field"]
    info = client.info(url)
    rights = edit_rights(info)
    rep = SyncReport()
    p = pending(path, table)
    ds = _open(path)
    rows, idx = _local_rows(ds, table, meta)
    base = _base(ds, table)
    ds = None
    edit_date = (meta["edit_fields"] or {}).get("editDateField")

    # what the server holds now for every feature changed or deleted here
    check = sorted({o for _f, o in p.updates} | set(p.deletes))
    current = _fetch(client, url, oid, check) if check else {}

    def conflict(o):
        b = base.get(o)
        s = current.get(o)
        if s is None:
            return "deleted on the server"
        sattrs = s.get("attributes") or {}
        if edit_date and b.get("edit_date") is not None and sattrs.get(edit_date) is not None:
            if int(sattrs[edit_date]) != int(b["edit_date"]):
                who = sattrs.get((meta["edit_fields"] or {}).get("editorField") or "", "")
                return "changed on the server" + (f" by {who}" if who else "")
            return None
        if _editable_values(meta, sattrs) != {k: _norm(v) for k, v in b["server"].items()} or \
                geom_hash(s.get("geometry")) != b["server_geom"]:
            return "changed on the server"
        return None

    adds, updates, deletes = [], [], []
    add_fids, theirs = [], []
    for fid in p.adds:
        if not rights["add"]:
            rep.refused.append(("add", fid))
            continue
        _o, vals, geom = rows[fid]
        adds.append({"attributes": _push_attrs(meta, vals), "geometry": geom})
        add_fids.append(fid)
    for fid, o in p.updates:
        why = conflict(o)
        if why and policy != MINE:
            if policy == THEIRS:
                theirs.append(o)
            else:
                rep.conflicts.append((fid, o, why))
            continue
        if why == "deleted on the server":        # "mine": the feature comes back as a new one
            if rights["add"]:
                _o, vals, geom = rows[fid]
                adds.append({"attributes": _push_attrs(meta, vals), "geometry": geom})
                add_fids.append(fid)
            continue
        if not rights["update"]:
            rep.refused.append(("update", fid))
            continue
        _o, vals, geom = rows[fid]
        b = base[o]
        a = _push_attrs(meta, vals)
        a = {k: v for k, v in a.items() if _norm(v) != _norm(b["local"].get(k))}   # only what changed
        a[oid] = o
        upd = {"attributes": a}
        if geom_hash(geom) != b["local_geom"] and rights["geometry"]:
            upd["geometry"] = geom
        updates.append(upd)
    for o in p.deletes:
        if o not in current:                      # already gone on the server
            rep.deleted += 1
            continue
        why = conflict(o)
        if why and policy != MINE:
            if policy == THEIRS:
                theirs.append(o)
            else:
                rep.conflicts.append((None, o, why + " (deleted here)"))
            continue
        if not rights["delete"]:
            rep.refused.append(("delete", o))
            continue
        deletes.append(o)

    done_oids, new_oids = set(), {}
    if adds or updates or deletes:
        if not any(rights.values()):
            raise OfflineError("The service does not allow editing: the changes stay in the GeoPackage.")
        res = client.apply_edits(url, adds, updates, deletes, rollback=False)
        for fid, r in zip(add_fids, res.get("addResults") or []):
            if r.get("success"):
                rep.added += 1
                new_oids[fid] = r.get("objectId")
            else:
                rep.errors.append((f"add {fid}", (r.get("error") or {}).get("description", "failed")))
        for u, r in zip(updates, res.get("updateResults") or []):
            if r.get("success"):
                rep.changed += 1
                done_oids.add(u["attributes"][oid])
            else:
                rep.errors.append((f"update {u['attributes'][oid]}", (r.get("error") or {}).get("description", "")))
        for o, r in zip(deletes, res.get("deleteResults") or []):
            if r.get("success"):
                rep.deleted += 1
            else:
                rep.errors.append((f"delete {o}", (r.get("error") or {}).get("description", "failed")))

    # bring the server's versions in: the features just sent (ids, editor tracking), "theirs" and, with pull,
    # every feature changed on the server and not edited here
    ds = _open(path)
    lyr = ds.GetLayerByName(table)
    for fid, o in new_oids.items():
        f = lyr.GetFeature(fid)
        f.SetField(oid, o)
        lyr.SetFeature(f)
    ds = None
    refresh = set(done_oids) | set(new_oids.values()) | set(theirs)
    if pull:
        everything = {f["attributes"][oid]: f for f in client.query_all(url, where=meta["where"], info=info,
                                                                         out_sr=meta["wkid"])}
        still_pending = ({o for _f, o in p.updates} | set(p.deletes)) - set(theirs)
        conflicted = {c[1] for c in rep.conflicts}
        for o, f in everything.items():
            b = base.get(o)
            if o in refresh or o in conflicted or (o in still_pending and o not in done_oids):
                continue
            if b is None:
                if o not in new_oids.values():
                    refresh.add(o)          # new on the server
                continue
            sattrs = f.get("attributes") or {}
            if _editable_values(meta, sattrs) != {k: _norm(v) for k, v in b["server"].items()} or \
                    geom_hash(f.get("geometry")) != b["server_geom"]:
                refresh.add(o)
        gone = [o for o in base if o not in everything and o not in still_pending and o not in conflicted]
        current.update({o: everything[o] for o in refresh if o in everything})
        rep.pulled = len([o for o in refresh if o not in done_oids and o not in new_oids.values()])
        _remove_local(path, table, oid, gone)
        rep.pulled += len(gone)
    else:
        missing = [o for o in refresh if o not in current]
        current.update(_fetch(client, url, oid, missing) if missing else {})
    deleted_theirs = [o for o in theirs if o not in current]
    _write_server(path, table, meta, {o: current[o] for o in refresh if o in current})
    _remove_local(path, table, oid, deleted_theirs)
    _snapshot(path, table, {o: current[o] for o in refresh if o in current}, only=refresh)
    gone_base = set(deletes) | set(deleted_theirs) | {o for o in p.deletes if o not in current}
    if gone_base:
        ds = _open(path)
        _clear_base(ds, table, gone_base)
        ds = None
    return rep


def _push_attrs(meta, vals):
    return {fd["name"]: vals.get(fd["name"]) for fd in meta["fields"] if fd["editable"]}


def _fetch(client, url, oid, oids):
    out = {}
    for k in range(0, len(oids), 200):
        chunk = oids[k:k + 200]
        d = client.query(url, objectIds=",".join(str(o) for o in chunk), outFields="*", returnGeometry="true")
        for f in d.get("features") or []:
            out[(f.get("attributes") or {}).get(oid)] = f
    return out


def _write_server(path, table, meta, server):
    """Put the server's version of these features into the GeoPackage (update or add)."""
    if not server:
        return
    ds = _open(path)
    lyr = ds.GetLayerByName(table)
    oid = meta["oid_field"]
    gtype = lyr.GetGeomType()
    by_oid = {}
    lyr.ResetReading()
    for f in lyr:
        if f.IsFieldSetAndNotNull(oid):
            by_oid[f.GetFieldAsInteger64(oid)] = f.GetFID()
    types = {fd["name"]: _TYPES.get(fd["type"]) for fd in meta["fields"]}
    lyr.StartTransaction()
    for o, sf in server.items():
        fid = by_oid.get(o)
        f = lyr.GetFeature(fid) if fid is not None else ogr.Feature(lyr.GetLayerDefn())
        attrs = sf.get("attributes") or {}
        for name, t in types.items():
            if name in attrs and t is not None and f.GetFieldIndex(name) >= 0:
                _write_value(f, name, t, attrs[name])
        gj = esri_to_geojson(sf.get("geometry"))
        if gj is not None:
            g = ogr.CreateGeometryFromJson(json.dumps(gj))
            f.SetGeometry(ogr.ForceTo(g, gtype) if g.GetGeometryType() != gtype else g)
        if fid is None:
            lyr.CreateFeature(f)
        else:
            lyr.SetFeature(f)
    lyr.CommitTransaction()
    ds = None


def _remove_local(path, table, oid, oids):
    if not oids:
        return
    ds = _open(path)
    lyr = ds.GetLayerByName(table)
    want = set(oids)
    gone = [f.GetFID() for f in lyr if f.IsFieldSetAndNotNull(oid) and f.GetFieldAsInteger64(oid) in want]
    for fid in gone:
        lyr.DeleteFeature(fid)
    _clear_base(ds, table, want)
    ds = None


def check_service(client, url):
    """-> (rights dict, sentence) for the UI before taking a layer offline or syncing."""
    try:
        info = client.info(url)
    except RestError as e:
        return {}, str(e)
    from .rest import edit_rights_text
    return edit_rights(info), edit_rights_text(info)
