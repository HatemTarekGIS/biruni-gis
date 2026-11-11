"""A small stand-in for ArcGIS Server, used by tests/test_arcgis_services.py (there is no live server).

    python arcgis_mock_server.py <port file>

Starts on a free port, writes it to <port file> and serves until killed:

    /arcgis/rest/info                          token service address
    /arcgis/tokens/generateToken               user "editor" / password "s3cret!"
    /arcgis/rest/services[/Folder]             folders and services
    /arcgis/rest/services/<name>/FeatureServer[/<id>]        service and layer information
    .../FeatureServer/<id>/query               where (1=1, field = value, field IN (...)), objectIds, outFields,
                                               returnIdsOnly, returnCountOnly, resultOffset/resultRecordCount
    .../FeatureServer/<id>/applyEdits          adds, updates, deletes (per-feature results)
    .../FeatureServer/<id>/addFeatures|updateFeatures|deleteFeatures
    /sharing/rest/oauth2/authorize|token       OAuth 2 with PKCE (the authorize step redirects at once)
    /__admin/edit                              changes a feature "as another user" (to make conflicts)
    /__admin/log                               the edit requests received

Services: Parcels (polygons, editor tracking, editable), Utilities/Water (valves = points, mains = lines,
editable), Utilities/Readonly (points, query only), Secure/Assets (points, needs a token) and Basemap (a
MapServer). Geometries are in Web Mercator (3857). Pure standard library."""
import base64
import copy
import hashlib
import json
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER, PASSWORD = "editor", "s3cret!"
TOKENS = {}                 # token -> user
CODES = {}                  # oauth code -> (challenge, client)
LOG = []
LOCK = threading.Lock()
SR = {"wkid": 102100, "latestWkid": 3857}


def now_ms():
    return int(time.time() * 1000)


def _fields(extra):
    base = [{"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID"}]
    return base + extra


def _layer(lid, name, gtype, fields, feats, caps, tracking=False):
    lyr = {"id": lid, "name": name, "type": "Feature Layer", "geometryType": gtype, "objectIdField": "OBJECTID",
           "globalIdField": "", "fields": fields, "capabilities": caps, "maxRecordCount": 2,
           "advancedQueryCapabilities": {"supportsPagination": lid != 0 or name != "Valves"},
           "supportsPagination": lid != 0 or name != "Valves",
           "extent": {"xmin": 3900000, "ymin": 2900000, "xmax": 3910000, "ymax": 2910000, "spatialReference": SR},
           "hasZ": False, "hasM": False, "allowGeometryUpdates": True, "supportsApplyEditsWithGlobalIds": False,
           "currentVersion": 11.1, "drawingInfo": {"renderer": {"type": "simple"}}}
    if tracking:
        lyr["editFieldsInfo"] = {"creationDateField": "created_date", "creatorField": "created_user",
                                 "editDateField": "last_edited_date", "editorField": "last_edited_user"}
    store = {}
    for k, (attrs, geom) in enumerate(feats, start=1):
        a = {"OBJECTID": k}
        a.update(attrs)
        if tracking:
            a.update(created_date=1700000000000, created_user="admin", last_edited_date=1700000000000,
                     last_edited_user="admin")
        store[k] = {"attributes": a, "geometry": geom}
    return {"info": lyr, "store": store, "next": len(feats) + 1}


def _square(x, y, d=100):
    return {"rings": [[[x, y], [x, y + d], [x + d, y + d], [x + d, y], [x, y]]]}


TRACK = [{"name": n, "type": t, "alias": n, "editable": False} for n, t in
         (("created_user", "esriFieldTypeString"), ("created_date", "esriFieldTypeDate"),
          ("last_edited_user", "esriFieldTypeString"), ("last_edited_date", "esriFieldTypeDate"))]

SERVICES = {
    "Parcels": {"type": "FeatureServer", "secure": False, "layers": [_layer(
        0, "Parcels", "esriGeometryPolygon",
        _fields([{"name": "parcel_no", "type": "esriFieldTypeString", "alias": "Parcel number", "length": 20},
                 {"name": "use", "type": "esriFieldTypeString", "alias": "Land use", "length": 30},
                 {"name": "area_m2", "type": "esriFieldTypeDouble", "alias": "Area"},
                 {"name": "surveyed", "type": "esriFieldTypeDate", "alias": "Surveyed"}] + TRACK),
        [({"parcel_no": f"P-{k}", "use": "residential" if k % 2 else "commercial", "area_m2": 10000.0,
           "surveyed": 1690000000000 + k * 86400000}, _square(3900000 + k * 200, 2900000)) for k in range(1, 6)],
        "Query,Create,Update,Delete,Editing,Sync", tracking=True)]},
    "Utilities/Water": {"type": "FeatureServer", "secure": False, "layers": [
        _layer(0, "Valves", "esriGeometryPoint",
               _fields([{"name": "valve_id", "type": "esriFieldTypeString", "alias": "Valve", "length": 10},
                        {"name": "diameter", "type": "esriFieldTypeInteger", "alias": "Diameter"}]),
               [({"valve_id": f"V{k}", "diameter": 100 + k}, {"x": 3900000 + k * 50, "y": 2900000 + k * 50})
                for k in range(1, 4)], "Query,Create,Update,Delete,Editing"),
        _layer(1, "Mains", "esriGeometryPolyline",
               _fields([{"name": "material", "type": "esriFieldTypeString", "alias": "Material", "length": 20}]),
               [({"material": "PVC"}, {"paths": [[[3900000, 2900000], [3901000, 2900500]]]})],
               "Query,Update,Editing")]},
    "Utilities/Readonly": {"type": "FeatureServer", "secure": False, "layers": [
        _layer(0, "Hydrants", "esriGeometryPoint",
               _fields([{"name": "hyd_id", "type": "esriFieldTypeString", "alias": "Hydrant", "length": 10}]),
               [({"hyd_id": "H1"}, {"x": 3900100, "y": 2900100})], "Query")]},
    "Secure/Assets": {"type": "FeatureServer", "secure": True, "layers": [
        _layer(0, "Assets", "esriGeometryPoint",
               _fields([{"name": "tag", "type": "esriFieldTypeString", "alias": "Tag", "length": 10}]),
               [({"tag": "A1"}, {"x": 3900200, "y": 2900200}), ({"tag": "A2"}, {"x": 3900300, "y": 2900300})],
               "Query,Create,Update,Delete,Editing")]},
    "Basemap": {"type": "MapServer", "secure": False, "layers": []},
}


def _find(path):
    """/arcgis/rest/services/<name>/<Type>[/<id>[/<op>]] -> (service, layer or None, op)."""
    m = re.match(r"^/arcgis/rest/services/(.+?)/(FeatureServer|MapServer)(?:/(\d+))?(?:/(\w+))?/?$", path)
    if not m:
        return None, None, None
    svc = SERVICES.get(m.group(1))
    if svc is None or svc["type"] != m.group(2):
        return None, None, None
    lyr = None
    if m.group(3) is not None:
        lid = int(m.group(3))
        lyr = next((x for x in svc["layers"] if x["info"]["id"] == lid), None)
        if lyr is None:
            return svc, False, None
    return svc, lyr, m.group(4)


def _where(where, attrs):
    w = (where or "1=1").strip()
    if w in ("1=1", "", "1 = 1"):
        return True
    m = re.match(r"^\s*(\w+)\s*=\s*'([^']*)'\s*$", w) or re.match(r"^\s*(\w+)\s*=\s*([-\d.]+)\s*$", w)
    if m:
        return str(attrs.get(m.group(1))) == m.group(2)
    m = re.match(r"^\s*(\w+)\s+IN\s*\(([^)]*)\)\s*$", w, re.I)
    if m:
        vals = [v.strip().strip("'") for v in m.group(2).split(",")]
        return str(attrs.get(m.group(1))) in vals
    m = re.match(r"^\s*(\w+)\s*>\s*([-\d.]+)\s*$", w)
    if m:
        return attrs.get(m.group(1)) is not None and float(attrs[m.group(1)]) > float(m.group(2))
    return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # -- plumbing --
    def _params(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
        if self.command == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8") if n else ""
            q.update({k: v[-1] for k, v in urllib.parse.parse_qs(body, keep_blank_values=True).items()})
        return u.path, q

    def _send(self, obj, code=200, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code, message, details=None):
        self._send({"error": {"code": code, "message": message, "details": details or []}})

    def _user(self, q):
        tok = q.get("token") or ""
        h = self.headers.get("X-Esri-Authorization") or self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer "):
            tok = tok or h[7:].strip()
        return TOKENS.get(tok)

    def do_GET(self):  # noqa: N802
        self._handle()

    def do_POST(self):  # noqa: N802
        self._handle()

    def _handle(self):
        path, q = self._params()
        try:
            with LOCK:
                self._route(path, q)
        except Exception as e:  # noqa: BLE001
            self._error(500, f"mock server error: {e!r}")

    # -- routes --
    def _route(self, path, q):
        if "/sharing/rest/" in path:        # a portal at any address (…/portal/sharing/rest/…)
            path = path[path.index("/sharing/rest/"):]
        if path == "/arcgis/rest/info":
            return self._send({"currentVersion": 11.1, "authInfo": {
                "isTokenBasedSecurity": True,
                "tokenServicesUrl": f"http://127.0.0.1:{self.server.server_address[1]}/arcgis/tokens/generateToken"}})
        if path in ("/arcgis/tokens/generateToken", "/sharing/rest/generateToken"):
            if q.get("username") != USER or q.get("password") != PASSWORD:
                return self._error(400, "Unable to generate token.", ["Invalid username or password."])
            tok = secrets.token_hex(16)
            TOKENS[tok] = USER
            return self._send({"token": tok, "expires": now_ms() + 3600 * 1000, "ssl": False})
        if path == "/sharing/rest/oauth2/authorize":
            code = secrets.token_hex(8)
            CODES[code] = (q.get("code_challenge"), q.get("client_id"))
            loc = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q.get("state", "")})
            self.send_response(302)
            self.send_header("Location", loc)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path == "/sharing/rest/oauth2/token":
            if q.get("grant_type") == "refresh_token":
                if not q.get("refresh_token", "").startswith("r-"):
                    return self._error(498, "Invalid refresh token")
            else:
                challenge, client = CODES.pop(q.get("code"), (None, None))
                verifier = q.get("code_verifier", "")
                calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if challenge is None or calc != challenge or client != q.get("client_id"):
                    return self._error(400, "Invalid authorization code")
            tok = secrets.token_hex(16)
            TOKENS[tok] = "oauth-user"
            return self._send({"access_token": tok, "expires_in": 1800, "refresh_token": "r-" + tok,
                               "username": "oauth-user"})
        if path == "/__admin/edit":
            svc, lyr, _op = _find(q["layer"])
            f = lyr["store"][int(q["oid"])]
            f["attributes"].update(json.loads(q.get("attributes", "{}")))
            if lyr["info"].get("editFieldsInfo"):
                f["attributes"].update(last_edited_date=now_ms() + 1000, last_edited_user="someone_else")
            return self._send({"ok": True})
        if path == "/__admin/log":
            return self._send({"log": LOG})
        if path == "/arcgis/rest/services" or path.startswith("/arcgis/rest/services/") and "/" not in path[22:] \
                and not any(path.endswith(t) for t in ("FeatureServer", "MapServer")):
            folder = path[len("/arcgis/rest/services"):].strip("/")
            names = [n for n in SERVICES if (n.rsplit("/", 1)[0] if "/" in n else "") == folder]
            folders = sorted({n.split("/")[0] for n in SERVICES if "/" in n}) if not folder else []
            if folder and not names:
                return self._error(404, "Folder not found")
            return self._send({"currentVersion": 11.1, "folders": folders,
                               "services": [{"name": n, "type": SERVICES[n]["type"]} for n in names]})
        svc, lyr, op = _find(path)
        if svc is None:
            return self._error(404, "Service not found")
        if svc["secure"] and not self._user(q):
            if q.get("token"):
                return self._error(498, "Invalid token.")
            return self._error(499, "Token Required")
        if lyr is False:
            return self._error(400, "Invalid layer")
        if lyr is None:
            if op:
                return self._error(400, "Unsupported operation")
            info = {"currentVersion": 11.1, "serviceDescription": "", "spatialReference": SR,
                    "capabilities": svc["layers"][0]["info"]["capabilities"] if svc["layers"] else "Map,Query",
                    "layers": [{"id": x["info"]["id"], "name": x["info"]["name"], "type": "Feature Layer",
                                "geometryType": x["info"]["geometryType"]} for x in svc["layers"]],
                    "tables": [], "maxRecordCount": 2,
                    "fullExtent": {"xmin": 3900000, "ymin": 2900000, "xmax": 3910000, "ymax": 2910000,
                                   "spatialReference": SR}}
            if svc["type"] == "MapServer":
                info.update(mapName="Basemap", singleFusedMapCache=False, supportedImageFormatTypes="PNG32,PNG,JPG")
            return self._send(info)
        info = lyr["info"]
        if not op:
            return self._send(info)
        if op == "query":
            return self._query(lyr, q)
        caps = {c.lower() for c in info["capabilities"].split(",")}
        if op in ("applyEdits", "addFeatures", "updateFeatures", "deleteFeatures"):
            if "editing" not in caps:
                return self._error(400, "Requested operation is not supported by this service.",
                                   ["The service is not editable."])
            user = self._user(q) or "anonymous"
            LOG.append({"op": op, "layer": info["name"], "params": {k: v for k, v in q.items() if k != "token"}})
            if op == "applyEdits":
                adds = json.loads(q.get("adds") or "[]")
                updates = json.loads(q.get("updates") or "[]")
                deletes = [int(x) for x in str(q.get("deletes") or "").replace("[", "").replace("]", "").split(",")
                           if x.strip()]
            else:
                adds = json.loads(q.get("features") or "[]") if op == "addFeatures" else []
                updates = json.loads(q.get("features") or "[]") if op == "updateFeatures" else []
                deletes = ([int(x) for x in str(q.get("objectIds") or "").split(",") if x.strip()]
                           if op == "deleteFeatures" else [])
            res = self._edit(lyr, caps, user, adds, updates, deletes)
            if op == "applyEdits":
                return self._send(res)
            key = {"addFeatures": "addResults", "updateFeatures": "updateResults",
                   "deleteFeatures": "deleteResults"}[op]
            return self._send({key: res[key]})
        return self._error(400, "Unsupported operation")

    def _query(self, lyr, q):
        info, store = lyr["info"], lyr["store"]
        oids = sorted(store)
        if q.get("objectIds"):
            want = {int(x) for x in q["objectIds"].split(",") if x.strip()}
            oids = [o for o in oids if o in want]
        oids = [o for o in oids if _where(q.get("where"), store[o]["attributes"])]
        if q.get("returnIdsOnly", "").lower() == "true":
            return self._send({"objectIdFieldName": "OBJECTID", "objectIds": oids})
        if q.get("returnCountOnly", "").lower() == "true":
            return self._send({"count": len(oids)})
        page = info["maxRecordCount"]
        off = int(q.get("resultOffset") or 0)
        n = min(int(q.get("resultRecordCount") or page), page)
        exceeded = False
        if "resultOffset" in q or "resultRecordCount" in q:
            exceeded = off + n < len(oids)
            oids = oids[off:off + n]
        elif len(oids) > page and not q.get("objectIds"):
            oids, exceeded = oids[:page], True
        out_fields = q.get("outFields") or "*"
        names = None if out_fields.strip() == "*" else {s.strip() for s in out_fields.split(",")} | {"OBJECTID"}
        feats = []
        for o in oids:
            f = store[o]
            attrs = {k: v for k, v in f["attributes"].items() if names is None or k in names}
            item = {"attributes": attrs}
            if q.get("returnGeometry", "true").lower() != "false":
                item["geometry"] = copy.deepcopy(f["geometry"])
            feats.append(item)
        return self._send({"objectIdFieldName": "OBJECTID", "geometryType": info["geometryType"],
                           "spatialReference": SR, "fields": info["fields"], "features": feats,
                           "exceededTransferLimit": exceeded})

    def _edit(self, lyr, caps, user, adds, updates, deletes):
        info, store = lyr["info"], lyr["store"]
        tracking = info.get("editFieldsInfo")
        names = {f["name"] for f in info["fields"]}
        res = {"addResults": [], "updateResults": [], "deleteResults": []}
        for f in adds:
            if "create" not in caps:
                res["addResults"].append({"success": False, "error": {"code": 1000,
                                                                      "description": "Adding is not allowed."}})
                continue
            oid = lyr["next"]
            lyr["next"] += 1
            attrs = {k: v for k, v in (f.get("attributes") or {}).items() if k in names and k != "OBJECTID"}
            attrs["OBJECTID"] = oid
            if tracking:
                attrs.update(created_date=now_ms(), created_user=user, last_edited_date=now_ms(),
                             last_edited_user=user)
            store[oid] = {"attributes": attrs, "geometry": f.get("geometry")}
            res["addResults"].append({"objectId": oid, "success": True})
        for f in updates:
            oid = (f.get("attributes") or {}).get("OBJECTID")
            if "update" not in caps:
                res["updateResults"].append({"objectId": oid, "success": False, "error": {
                    "code": 1000, "description": "Updating is not allowed."}})
                continue
            if oid not in store:
                res["updateResults"].append({"objectId": oid, "success": False, "error": {
                    "code": 1019, "description": "Object is missing."}})
                continue
            cur = store[oid]
            for k, v in (f.get("attributes") or {}).items():
                if k in names and k != "OBJECTID":
                    cur["attributes"][k] = v
            if f.get("geometry"):
                cur["geometry"] = f["geometry"]
            if tracking:
                cur["attributes"].update(last_edited_date=now_ms(), last_edited_user=user)
            res["updateResults"].append({"objectId": oid, "success": True})
        for oid in deletes:
            if "delete" not in caps:
                res["deleteResults"].append({"objectId": oid, "success": False, "error": {
                    "code": 1000, "description": "Deleting is not allowed."}})
                continue
            ok = store.pop(oid, None) is not None
            res["deleteResults"].append({"objectId": oid, "success": ok} if ok else {
                "objectId": oid, "success": False, "error": {"code": 1019, "description": "Object is missing."}})
        return res


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with open(sys.argv[1], "w") as f:
        f.write(str(srv.server_address[1]))
    srv.serve_forever()


if __name__ == "__main__":
    main()
