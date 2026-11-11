# Map service in one folder (BMS1)

What a map server does, written once as files. Any static host serves it: GitHub Pages, Netlify, a shared folder
behind the office web server, a USB stick with any small server. No licence, no account, no process running.

## API

```python
from biruni.mapservice import publish, verify, rebase
info = publish(layers, out_dir, title, options)        # -> dict (below)
check = verify(folder_or_url, expected_pub=None)       # -> dict(ok, signature_ok, signer, fingerprint, bad, missing…)
rebase(out_dir, "https://user.github.io/my-map")       # OGC/TileJSON links -> that address, signed again
from biruni.mapservice.dialog import open_dialog, check_ui   # the two commands
```

`options` (all optional): `description`, `min_zoom` 0, `max_zoom` 14, `ogc` True, `page_size` 1000, `xyz` False,
`sign` True, `signer` (default: project author, else the Windows user), `hide` {layer name or id: [fields]},
`raster` (one GDAL raster layer), `raster_max_zoom`, `basemap` "osm" | "none", `basemap_pack` (a PMTiles basemap
cut to the area of the data), `base_url` (the final address: OGC links become absolute).

`publish` returns `index, folder, layers [{id, name, count, fgb, collection}], raster, files [(path, bytes)],
signed, pub, fingerprint, link_suffix ("#fp=<16 hex>"), warnings, bounds, ogc, tilejson`.

## The folder

| Path | What |
|---|---|
| `index.html` | the viewer |
| `service.json` | what the service holds, for programs |
| `manifest.json` + `manifest.sig` | every file's SHA-256 and size; ed25519 signature of the exact bytes of manifest.json |
| `data/map.pmtiles` | ONE archive: the vector tiles of every layer (source-layer = layer id; `_s` style, `_l` label, `_fid`) |
| `data/<id>.fgb` | FlatGeobuf per layer (WGS 84, multi-geometries, packed Hilbert R-tree), all published fields + `_fid` |
| `data/raster.pmtiles` | optional: one raster as PNG tiles (EPSG:3857), overviews down to a few tiles |
| `data/basemap.pmtiles` | optional: the basemap pack cut to the data |
| `ogc/…` | static OGC API - Features (below) |
| `xyz/{z}/{x}/{y}.pbf` + `tiles.json` | optional: the same tiles as plain files (not gzipped) and a TileJSON 3.0.0 |
| `lib/` | MapLibre GL JS 6.16 (BSD-3), pmtiles.js 4.5 (BSD-3), flatgeobuf.js 4.4.0 (BSD-3), Noto Sans Arabic (OFL) |

Layer ids are ASCII (`layer<n>` when the name has no Latin letters) so every address works on every host.

## What is published, what is not

* Only what the layer's style draws (a hidden category is not published).
* Only the fields of the pop-ups: fields hidden in the attribute form and the internal key are left out, and so is
  every field named in `hide`. They are in no file: not the tiles, not the FGB, not the OGC pages, not the page.
* The tiles carry no attributes but `_s`, `_l`, `_fid`: identify reads the feature from the FGB.

## The viewer

* The map: MapLibre draws `data/map.pmtiles` (one source, one source-layer per layer), the raster, the basemap.
* Identify: the click finds the drawn feature, then reads it from the layer's FGB with a range request on the
  click's box (the R-tree, then only that feature's bytes) and shows every published field.
* Query: layer, field, condition (equals, is not, contains, starts with, greater than, less than: numbers compared
  as numbers), value, "only in the current view" (a bbox read through the R-tree). The whole layer is read in
  ranges when the view is not used. The first 500 are listed and drawn; the count is exact.
* Without WebGL the map is not drawn (a message says so); the layers, queries and the signature still work.
* Signature: the page fetches manifest.json and manifest.sig, checks Ed25519 with Web Crypto, then checks every
  file up to 5 MB against its SHA-256 ("Check all files" checks the big ones too). Badge: green signed by …,
  red altered (lists the files) / bad signature / another key than the link's `#fp=`, amber when the browser
  cannot check (Web Crypto needs https or localhost, and a browser with Ed25519) or when the folder is unsigned.
* Arabic and RTL: the page takes the UI language; titles are `unicode-bidi: plaintext`.

## Static OGC API - Features: what it can and cannot do

Files, where clients build their addresses (QGIS builds `<collections>/<id>` and `<collection>/items` itself and
does not resolve relative links; a static host serves a folder's `index.html` for `…/folder/`):

```
ogc/index.html (= index.json)         landing page (JSON)
ogc/api/index.html                    OpenAPI 3.0 (declares limit = the page size)
ogc/conformance/index.html            core, geojson, oas30
ogc/collections/index.html
ogc/collections/<id>/index.html
ogc/collections/<id>/items            page 1 (a file without extension)
ogc/collections/<id>/items-<k>.json   pages 2…, chained by next/prev links
ogc/about.html                        a page for people
```

* It can: be added in QGIS (OAPIF, tested: 2,500 features over 3 pages with the Arabic field names) and in any
  client that follows links; serve every feature, paged; describe extents and CRS84.
* It cannot: answer a query. `bbox=`, `limit=`, `datetime=`, CQL filters, `/items/{featureId}` are ignored by a
  static host, which returns page 1 whatever the query. A client that sends a bbox gets the whole page and must
  filter itself (QGIS keeps the features, which is right for display but downloads all pages).
* Links must be absolute for GIS programs. With `base_url` they are written absolute; without it they are relative
  (fine for browsers) and `rebase(folder, address)` rewrites them later and signs again (the dialog's "Copy the
  OGC address" does it, for the preview server when no address is given).
* JSON is served as `text/html` (index.html) or `application/octet-stream` (items) by static hosts; QGIS accepts
  both. Strict clients that check the media type will refuse it.
* No transactions (POST/PUT/DELETE): a static host is read-only.

## The signature, honestly

The manifest is signed with this PC's dashboard key (`dashboard.cards.seed()`, kept encrypted in the settings).
`verify()` on the desktop is the trustworthy check: it compares the key with this PC's and re-hashes every file.
The page's check guards against a changed data file or a corrupted copy, not against someone who replaces the
whole folder, the page included: such a copy can carry another key. The link pin `#fp=<16 hex of the key>` makes
that visible to whoever opens the shared link, and the fingerprint shown is the one to compare by phone.

## Not done

* Private layers (encrypted with AES-GCM, key after `#`): not built.
* The page in a real browser with WebGL was not driven by the tests (the offscreen test browser has no WebGL):
  identify on the drawn map is untested there; the FGB queries, the signature check and the badge are tested.
