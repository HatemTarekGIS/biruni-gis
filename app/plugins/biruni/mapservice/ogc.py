"""A static OGC API - Features tree (Part 1: Core, GeoJSON), written once as files.

A static host has no code: it cannot read the query of a request (bbox=, limit=, filters) - it hands back the
same file whatever is asked. So the tree is what a server answers with no query at all: the landing page, the
API definition, conformance, the collections, and each collection's items in pages chained by "next" links. A
client that follows the links reads every feature; a client that adds a bbox gets the whole page and keeps the
features it wants itself.

The files sit where clients build their addresses, not only where links point (QGIS, for one, builds
<collections>/<id> and <collection>/items itself and does not resolve relative links):

    ogc/index.html                     the landing page (JSON; a static host serves a folder's index.html)
    ogc/index.json                     the same, under a name a browser shows as JSON
    ogc/api/index.html                 the API definition (OpenAPI 3.0)
    ogc/conformance/index.html
    ogc/collections/index.html         the collections
    ogc/collections/<id>/index.html    a collection
    ogc/collections/<id>/items         page 1 of its features (a file with no extension: <collection>/items)
    ogc/collections/<id>/items-<k>.json   the next pages

Links are absolute when the final address is known (base_url) - GIS programs need that - else relative (they
work in browsers and in clients that resolve links; ``core.rebase`` rewrites them for an address later).
"""
import datetime
import html
import json
import os

CRS84 = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
CONFORMS = ["http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
            "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson",
            "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/oas30"]
JSON = "application/json"
GEOJSON = "application/geo+json"
OPENAPI = "application/vnd.oai.openapi+json;version=3.0"
LANDING = "ogc/"


def _write(folder, rel, obj):
    p = os.path.join(folder, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return rel


def write(folder, title, description, collections, page_size=1000, base_url=""):
    """folder: <service>/ogc. collections: [dict(id, title, bbox [w, s, e, n], features: [GeoJSON features with
    "id"], fields)]. -> the relative paths written (under folder)."""
    page_size = max(1, int(page_size))
    root = (base_url.rstrip("/") + "/ogc/") if base_url else None
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def href(frm_dir, target):
        """target: a path under ogc/ ("" = the landing folder); frm_dir: the folder of the document."""
        if root:
            return root + target
        up = "../" * len([p for p in frm_dir.split("/") if p])
        return (up + target) or "./"

    def link(frm_dir, target, rel, typ=JSON, title_=None):
        d = {"href": href(frm_dir, target), "rel": rel, "type": typ}
        if title_:
            d["title"] = title_
        return d

    done = []
    lim = {"name": "limit", "in": "query", "required": False, "style": "form", "explode": False,
           "schema": {"type": "integer", "minimum": 1, "maximum": page_size, "default": page_size}}
    ok = {"200": {"description": "OK", "content": {JSON: {}}}}
    paths = {"/": {"get": {"summary": "landing page", "operationId": "getLandingPage", "responses": ok}},
             "/conformance": {"get": {"summary": "conformance", "operationId": "getConformance", "responses": ok}},
             "/collections": {"get": {"summary": "collections", "operationId": "getCollections", "responses": ok}}}
    for c in collections:
        paths[f"/collections/{c['id']}"] = {"get": {"operationId": f"describe_{c['id']}", "responses": ok}}
        paths[f"/collections/{c['id']}/items"] = {"get": {
            "operationId": f"items_{c['id']}", "parameters": [lim],
            "responses": {"200": {"description": "the features", "content": {GEOJSON: {}}}}}}
    done.append(_write(folder, "api/index.html", {
        "openapi": "3.0.3", "info": {"title": title, "version": "1.0",
                                     "description": "A static service: every request gets the same file (no query)."},
        "servers": [{"url": root.rstrip("/") if root else "."}], "paths": paths}))
    landing = {"title": title, "description": description or title,
               "links": [link("", "", "self", JSON, "this document"),
                         link("", "api/", "service-desc", OPENAPI, "the API definition"),
                         link("", "conformance/", "conformance", JSON, "conformance"),
                         link("", "collections", "data", JSON, "collections")]}
    done.append(_write(folder, "index.html", landing))
    done.append(_write(folder, "index.json", landing))
    done.append(_write(folder, "conformance/index.html", {"conformsTo": CONFORMS}))
    cols = []
    for c in collections:
        cid = c["id"]
        cdir = f"collections/{cid}"
        n = len(c["features"])
        pages = max(1, (n + page_size - 1) // page_size)
        page_name = (lambda k: f"{cdir}/items" if k == 1 else f"{cdir}/items-{k}.json")
        col = {"id": cid, "title": c.get("title") or cid, "itemType": "feature", "crs": [CRS84],
               "extent": {"spatial": {"bbox": [list(c["bbox"])], "crs": CRS84}},
               "links": [link(cdir, cdir + "/", "self"),
                         link(cdir, page_name(1), "items", GEOJSON, "the features (page 1 of %d)" % pages)]}
        done.append(_write(folder, cdir + "/index.html", col))
        listed = dict(col)
        listed["links"] = [link("collections", cdir + "/", "self"), link("collections", page_name(1), "items", GEOJSON)]
        cols.append(listed)
        for k in range(1, pages + 1):
            me = page_name(k)
            feats = c["features"][(k - 1) * page_size:k * page_size]
            links = [link(cdir, me, "self", GEOJSON), link(cdir, cdir + "/", "collection")]
            if k < pages:
                links.append(link(cdir, page_name(k + 1), "next", GEOJSON))
            if k > 1:
                links.append(link(cdir, page_name(k - 1), "prev", GEOJSON))
            done.append(_write(folder, me, {"type": "FeatureCollection", "features": feats, "links": links,
                                            "numberMatched": n, "numberReturned": len(feats), "timeStamp": stamp}))
    done.append(_write(folder, "collections/index.html", {
        "collections": cols, "links": [link("collections", "collections", "self"), link("collections", "", "up")]}))
    rows = ""
    for c in collections:
        rows += '<li><a href="%s">%s</a> (%d)</li>' % (html.escape(href("", "collections/%s/" % c["id"])),
                                                       html.escape(c.get("title") or c["id"]), len(c["features"]))
    with open(os.path.join(folder, "about.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                f"<title>{html.escape(title)} - OGC API - Features</title>"
                '<body style="font:15px Segoe UI,Tahoma,sans-serif;max-width:720px;margin:2em auto;padding:0 16px">'
                f"<h1>{html.escape(title)}</h1><p>OGC API - Features (static). Add the address of this folder "
                "(…/ogc/) in a GIS program. "
                '<a href="index.json">landing page</a> · <a href="api/">API</a> · '
                '<a href="conformance/">conformance</a> · <a href="collections/">collections</a></p>'
                f"<ul>{rows}</ul></body>")
    done.append("about.html")
    return done
