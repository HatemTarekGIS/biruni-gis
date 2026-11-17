# Biruni live dashboard — contract between the parts

A dashboard runs on this PC (no server of ours or anyone's). Phones, tablets and other PCs open it in a
browser and see it live. Three parts, three owners; this file is the contract between them.

* engine (model.py, data.py, cards.py, seals.py, service.py) — what a dashboard is, its numbers, who sees what
* page   (page.py → one self-contained HTML/JS/CSS, every language inside) — draws a dashboard in a browser
* desk   (server.py, host.py, builder.py, dock.py) — builder on the PC, sharing, links/QR, transports

## 1. Dashboard definition (JSON, stored in the project's GPKG table `biruni_dashboards` (id, title, def
JSON, updated) or, with no GPKG, in the project file's custom properties)

```json
{"v": 1, "id": "d_8f3a", "title": "Survey progress", "theme": "biruni-light", "columns": 12,
 "dates": "both",                       // greg | hijri | both
 "k_min": 0,                            // groups with fewer features than this are hidden (privacy); 0 = off
 "sources": {"s1": {"layer": "<QgsMapLayer id>", "filter": "<QGIS expression or ''>"}},
 "filters": [{"id": "f1", "source": "s1", "field": "district", "label": "District"}],
 "widgets": [{"id": "w1", "type": "kpi", "title": "Parcels done", "x": 0, "y": 0, "w": 3, "h": 2,
              "source": "s1",
              "measure": {"op": "count", "field": null, "where": "status = 'done'"},
              "group": null,
              "style": {"color": "#2e7d32", "icon": "check", "unit": "", "decimals": 0, "target": 1200,
                        "thresholds": [{"at": 900, "color": "#f9a825"}]},
              "options": {}}]}
```

measure.op: count | sum | avg | min | max | distinct | ratio (options.denominator: another measure).
group: {"field": f} | {"field": f, "time": "day|week|month|year"} | {"zones": "<polygon layer id>",
"label": field} (spatial group) — plus "top": n, "order": "value|key".
`where` and source `filter` are QGIS expressions, evaluated on the PC only.

Widget types (phase 1, 15): kpi, gauge, sparkline, line, area, bar, pie, table, map, choropleth,
progress, leaderboard, feed, text, filter.

## 2. Messages (JSON; the same over every transport)

browser → PC
* `{"t":"hello","card":"<card>","lang":"ar"}`
* `{"t":"filter","values":{"f1":["North"]}}`   (null clears)
* `{"t":"explain","widget":"w3","key":"North"}`  (what is behind a number)
* `{"t":"seal","code":"K7QM-2D9X"}`             (the signed statement behind a seal code)
* `{"t":"ping"}`

PC → browser
* `{"t":"dash","def":{…the definition, cut to the card: only its widgets, no sources/expressions/layer
  ids…},"card":{"name":"…","exp":…,"zone":true|false},"pc":"<pc name>","pub":"<hex ed25519>"}`
* `{"t":"data","rev":17,"ts":1762600000.0,"widgets":{"w1":{…shape below…,"seal":"K7QM-2D9X"}}}`
  rev grows by one each time any number changes; only changed widgets are sent after the first.
* `{"t":"explain","widget":"w3","key":"North","count":42,"bbox":[w,s,e,n],"features":[{"id":…,
  "label":…,"by":…,"when":…}],"more":true}`
* `{"t":"seal","code":"…","statement":{…},"sig":"<hex>"}`
* `{"t":"error","why":"expired|revoked|bad card|busy|…"}` · `{"t":"bye"}`

Widget data shapes
* kpi `{"value":1043,"prev":1001,"delta":42}` · gauge `{"value":..,"min":0,"max":..}`
* sparkline, line, area `{"series":[{"name":"…","points":[[x,y],…]}],"x":"time|number|key"}` (time = epoch s)
* bar, pie, leaderboard `{"items":[{"key":"North","label":"North","value":12,"hidden":false}]}`
* table `{"columns":[{"key":"a","label":"…"}],"rows":[["…",…]],"total":n}` (≤200 rows)
* map `{"geojson":{FeatureCollection EPSG:4326, ≤5000 features, simplified},"bbox":[…]}` — properties
  only those the card may see; style in the definition (style.color / style.by field)
* choropleth `{"geojson":{polygons with properties.value, properties.label},"min":..,"max":..}`
* progress `{"items":[{"label":"Zone 1","done":40,"total":55}]}`
* feed `{"items":[{"ts":…,"who":"Mona","what":"edited parcel 45","layer":"Parcels"}]}` (team room
  stream when the project is in a room, else layer edit signals)
* text `{"text":"…"}` (plain text with **bold** and line breaks only — never HTML)
* filter `{"options":[{"key":"North","label":"North","count":12}],"current":[…]}`
A value withheld by k_min: `{"hidden":true}` in its place (item, row, or whole widget).

## 3. Cards (who sees what) — `cards.py`

A card is a signed, self-contained permission: `BDC1.<b64u(payload JSON)>.<b64u(ed25519 sig)>`.
payload: `{"v":1,"id":"c_…","dash":"d_8f3a","name":"Supervisor North","widgets":"*"|[ids],
"hide":[field names],"zone":"<WKT EPSG:4326>"|null,"filters":{"f1":["North"]}|null (forced),
"input":false,"iat":…,"exp":…}`. Signed with the dashboard key of this PC (ed25519 seed kept encrypted
for the Windows user in settings, like the team identity). The PC refuses expired, revoked
(table `biruni_dashboard_revoked`) or foreign cards. The zone cuts every source to the features that
intersect it BEFORE any number is computed; hidden fields never leave the PC.
The link a viewer gets = page address + `#c=<card>&k=<transport key>&l=<lang>` (after "#": never sent
to any server).

## 4. Seals — `seals.py`

Every number sent carries a short seal code (Crockford base32, 8 chars, `XXXX-XXXX`). The PC keeps a
log (SQLite in settings/biruni/dashboard_seals.sqlite): code → statement
`{"dash","widget","title","value"(as sent),"filters","card","ts","data":<fingerprint of the source
features (fingerprint/core.py root of the rows used) >}` and its ed25519 signature. Tapping a number
on the phone shows the seal; "Check a seal" in Biruni (or any Biruni with the PC's public key and the
statement as a QR/text) verifies it, and — when the data is at hand — recomputes the value.

## 5. Transports (desk)

* LAN: HTTPS on the PC (the phone hub's certificate machinery), page at `/d/<token>`, messages by
  `POST /d/<token>/m` and server events by long-poll `GET /d/<token>/ev?rev=n`.
* Anywhere: the Biruni bridge (field_photos/bridge.py; WebRTC over Nostr/ntfy signalling) with a
  hidden host page like live_share; the public page is `build/bridge-page/d/index.html`
  (https://biruni-gis.com/d/), published by the user's own push.
* Inside Biruni: the builder's preview is the same page in QtWebEngine, fed in-process.

The page never trusts anything but the PC; the PC never trusts the page: every message is checked
against the card.

## Changes

* (desk, fork G) What the desk calls in service.py — all in Biruni's main thread:
  * `svc = DashboardService(project=None)` — reads dashboards with model.load_all(project).
  * `sess = svc.connect(send, transport="lan"|"bridge"|"preview")` — send(msg: dict) is called (main
    thread) for every PC → browser message; returns a session object with `sess.receive(msg: dict)`
    (browser → PC message), `sess.close()`, and after a good hello `sess.card` (the card payload dict)
    and `sess.dash_id`.
  * `svc.viewers()` → [{"card": id, "name": …, "dash": id, "transport": …, "since": ts}]
  * `svc.reload()` after the builder saves; `svc.stop()`.
  * Preview (builder): `svc.connect(send, transport="preview", definition=<unsaved def>)` takes the
    definition being edited, no card needed (the hello carries `"card": null`).
  * cards.py: `issue(dash_id, name, widgets="*", hide=(), zone_wkt=None, filters=None, exp=None) -> str`,
    `parse(card) -> payload | raises`, `revoke(card_id)`, `revoked()`; seals.py: `lookup(code)` →
    {"statement","sig"} | None and `verify(statement, sig, pub) -> bool`.
* (desk) The page's transport argument: `page_html(lang, transport)` with transport "lan" (fetch
  `m` / long-poll `ev` relative to the page's own address, session id `s` returned by the first
  POST), "bridge" (WebRTC via field_photos/bridge.py core; messages are the same JSON over the data
  channel) or "preview" (window.BiruniDash.feed(msg) in, window.BiruniDash.onSend(msg) out).
* (desk) LAN wire: `POST <page>/m` body `{"s": sid|null, "m": msg}` → `{"s": sid, "out": [msgs]}`;
  `GET <page>/ev?s=sid&n=<count seen>` → `{"out": [msgs], "n": count}` (long poll ≤25 s).
* (desk) Over the bridge data channel a PC → browser message longer than 48 000 characters is sent in
  parts: `{"t":"part","id":"<msg id>","i":0,"n":3,"d":"<slice of the JSON text>"}`; the page joins
  the n slices in order and handles the whole message. Browser → PC messages are small (≤16 KB).
* (desk) model.py: `load_all(project) -> [def]`, `save(project, definition)`, `delete(project, dash_id)`,
  `new_id() -> "d_…"`, `validate(definition) -> list of problems` (empty = fine).
* (desk) LAN wire, exactly: only the FIRST `POST m` (no "s": the hello) carries `"out"` and `"n"` in its
  answer; every later answer arrives by `GET ev` only, so nothing arrives twice. The page keeps the
  largest "n" it got and sends it back in the next `ev`. `POST bye {"s": sid}` ends a session.
* (engine, fork E) Additions, all optional for the page (nothing above changed):
  * The definition a viewer gets: a time-grouped widget carries `"time": "day|week|…"`; each filter is
    `{"id","label","locked"}` (locked = forced by the card); `options` keeps only display keys.
  * Data: `map` and `explain` add `"total"` (features before the 5000 cut); a `filter` widget forced by the
    card adds `"locked": true`; `feed` items add `"kind": "add|edit|del"` and `"fid"`; a widget that failed
    is `{"error": "…"}` (no seal). Series points withheld by k_min are `[x, null]`.
  * The seal statement's `"data"` is `"<16 hex of the field names>:<64 hex>"`: the sum mod 2^256 of the
    sha256 of each row (fid and the values the dashboard reads) - not the fingerprint tree, so it updates at
    once after an edit and is the same in any order. `seals.as_text(code)` / `from_text(text)` carry a seal
    as `BDS1.<statement>.<sig hex>`; `seals.recheck(statement, project)` computes the number again.
  * service: `svc.preview(session, definition)` when the builder changes the previewed definition;
    `svc.flush()` applies gathered edits at once (tests); a card revoked or expired while connected gets
    `{"t":"error","why":"revoked|expired"}` then `{"t":"bye"}` (checked every 5 s).
  * cards: `issued(dash_id)` lists the cards made (table `biruni_dashboard_cards`), `public_hex()`.
* (page, fork F) What the page adds on its side — nothing the PC must change; all optional:
  * the page sends `{"t":"ping"}` every 20 s over the bridge; any message from the PC counts as "alive" (a
    `{"t":"pong"}` answer is welcome, never required).
  * after a language change the page sends `{"t":"hello","card":…,"lang":…,"again":true}` on the same
    session (LAN: the same `s`); the PC may answer with the dash again in that language, or ignore it.
  * a `filter` widget names its filter in `options.filter` (a `filters[].id`); without it the page uses the
    first entry of `def.filters`.
  * `style.thresholds`: a value below `at` takes that colour, the nearest `at` above the value wins
    (`style.invert: true` = above); `style.invert` also turns a rising delta red. `options.orient` "h"|"v" for
    bar (default: "h" when more than 6 items or long labels), `options.donut:false` for a full pie,
    `options.stacked:false` for overlapping areas, `options.compact:true` for 1.2K-style KPI numbers,
    `options.colors:"each"` for one colour per bar. `style.icon` (kpi): check, users, clock, alert, map, flag,
    chart, star, home, ruler, parcel, money, camera, pin.
  * a big message over the bridge may come in pieces: `{"t":"part","id":"…","i":0,"n":3,"s":"<part of the
    JSON text>"}` - the page joins them in order and handles the whole message (the PC needs it only for
    messages larger than a data channel takes at once, ~256 KB).
  * the bridge offer carries `"app":"dash"` so a host page that serves several kinds of page can tell them apart.
  * the seal QR code holds `{"code","statement","sig","pub"}` as JSON (shown up to 1,600 bytes; beyond, Copy only).
* (desk) Builder preview: QtWebEngine inside Biruni's own process dies under QgsApplication (even a bare
  QWebEngineView with setHtml, offscreen), and Biruni keeps browser pages out of its process anyway (the bridge's
  hidden page has its own). So the preview is the LAN page opened in the system browser or on a phone, with the
  card `PREVIEW.<key>` (16–64 characters after the dot): the gateway connects such a hello to
  `svc.connect(send, transport="preview", definition=<the builder's definition>)` and calls
  `svc.preview(session, definition)` on every edit; closing the builder sends `error ended` + `bye`. An unknown
  key gets `error ended`. The page needs nothing special (it is the "lan" page; "preview" stays for in-process use).

### map v2 — the map is the hero (fork H, 2026-11-08)

* **Definition.** A map may list its layers in `options.layers` (old single-source maps keep working unchanged):
  `[{"source", "name", "where", "style": {"kind": "single|categories|graduated|layer", "field", "palette",
  "colors": {value: colour}, "classes", "color", "outline", "opacity", "width", "size"}, "label": field|null,
  "popup": [fields]}]`, plus `options.basemap: "none" | "project"`. `kind: "layer"` = the colours QGIS itself
  gives each feature with the layer's own renderer (categorized, graduated, rule-based...). `model.validate`
  checks the layers (source, where, style); `widget_fields` adds the layers' `where` and source filters.
* **Data** (mapdata.py): `{"layers": [{"id": "L<n>", "name", "kind": "fill|line|point", "style": {"color",
  "opacity", "outline", "width", "size"}, "legend": [{"label", "color", "key"?, "count"?}], "label": bool,
  "popup": [{"key", "label"}], "keys": [fields], "total", "geojson"}], "bbox", "total", "basemap": bool}`.
  Each feature's properties: `id`, `_c` (fill/line colour), `_o` (outline, style "layer"), `_l` (label text),
  `_k` ({field: group key} for the fields other widgets of that source group by, and its filters' fields), and
  the popup fields. A layer with fewer than k_min features is `{"id","name","kind","hidden":true}`. Hidden
  fields never appear as a label, popup field, key or colour: a style or renderer that reads one falls back
  to one colour. Geometries are cached per layer (simplified once) and dropped on edits like the rows.
* **Cross-filter keys.** bar, pie, leaderboard and progress data carry `"xf": <group field>` (field groups only,
  not time or zones, never a hidden field); progress items carry `key`; filter data carries `"xf": <field>`.
  The page selects features whose `_k[xf] == item key` on every map (pure client side).
* **Explain a map feature**: `{"t":"explain","widget","key":"L<n>:<fid>"}` → one feature with `fields:
  [{"label","value"}]` (popup fields minus hidden ones); a feature outside the card's zone is refused.
* **Basemap**: browser → PC `{"t":"basemap","widget","bbox":[w,s,e,n] (4326),"w","h","d"}` → PC → browser
  `{"t":"basemap","widget","bbox":<the extent drawn>,"img":"data:image/jpeg;base64,…" | null}` (basemap.py).
  Only the project's visible raster and vector-tile layers are drawn (never a vector layer); at most 1600 px a
  side, 2 renders at once, a viewer's newer view replaces its waiting one; a zone card's image is cut to its
  zone plus 25 %. A map without `options.basemap == "project"` gets `{"t":"error","why":"no basemap"}`.
* **Page**: layers drawn in order fill → line (with casing) → point; colours change with a pulse; labels in
  screen pixels, largest first, never overlapping (line labels along the line, polygons only when they fit);
  legend with layer switches (collapsed under 520 px), scale bar, north arrow, zoom at the cursor, double
  click, fit all; a tap = popup (popup fields, "Zoom to", "What is behind it") + highlight + selection of its
  group in the charts; a first tap on a bar/pie/leaderboard/progress item selects (maps dim the rest, a pill
  at the top clears it), a second tap asks what is behind it. `window.BiruniDash.setXF(field, key)`.
* **Builder**: a "Map layers" panel (add/remove/order layers, style kind, field, classes, colour, opacity,
  label, popup fields, basemap switch); new maps start with their layer in its own style over the basemap;
  template "Map dashboard" (8×8 map with the zones' outlines, numbers and a filter beside it, charts below).

### 11-09: the PC's identity on the bridge, and a new paper address

* Every phone holds the bridge key `k`, so `k` alone cannot tell the PC from another holder of the link answering in
  its place. The PC now has an ECDSA P-256 identity per key (`field_photos/bridge.py` `identity(key)`, kept with
  DPAPI next to the key, so a stable paper key keeps a stable identity). The hidden host page gets the private key
  in its own address (`&s=`); every answer it sends is signed (`ip`, `is` over `"BRA1|"+sid+"|"+sdp`).
* Links made now carry a pin of the public key (`&i=`, 22 characters): `viewer_url`, `page_url`, paper links. A page
  with a pin (kept in localStorage per key, so a page that rebuilds its address keeps it) refuses an answer the PC
  did not sign. **Papers and links made before carry no pin and work as before, unchecked**; print again to get
  the check.
* "New paper address…" (More menu, `ext_accounts.ACTIONS`): forgets the kept LAN token, bridge key and identity
  (`accounts.reset_stable()`), after a warning that every paper printed before stops showing live data.

## Extensions (11-08, round 3)

Seven modules add features without touching page.py, service.py or dock.py:
`ext_link` (dashboard in a link), `ext_mesh` (viewers relay to viewers), `ext_sound` (share by sound),
`ext_paper` (the live paper), `ext_show` (phone wall, narrative + voice, forecast, project film, meeting mode),
`ext_guard` (per-card invisible watermark), `ext_multi` (dashboard of dashboards).

A module `dashboard/ext_<name>.py` may define:
* `CSS`, `HTML` (inserted before the page script), `JS` (runs after the app, wrapped in its own function),
  `TEXTS = {lang: {key: text}}` for en, ar, es, fr, pt_BR, tr, fa (merged into the page texts; the JS reads
  them with `BiruniDash.t(key)`). Prefix keys and CSS classes and element ids with the module name.
* PC message handlers: `from .service import handler` then `@handler("paper_x") def f(service, session, msg)`
  — reply with `session._out({...})`; the session's card is `session.card` (payload dict or None for preview).
  Message types are prefixed with the module name.
* `ACTIONS = [(id, label_en, tabler_icon, fn(plugin, dock))]` — shown by the dashboard panel (dock) in its
  "More" menu; labels go through tr() (add them to your strings file).

Page API (window.BiruniDash): `send(obj)`, `on(type, fn)` (PC messages of that type), `onData(fn)` (after every
data message), `onBuild(fn)` (after the dashboard is built), `onLang(fn)`, `t(key)`, `lang()`, `rtl()`,
`state` (S: def, data, card, pc, pub, rev…), `widgets()`, `sheet(title, element)`, `close()`, `toast(text)`,
`button(id, label, svgPath, onclick)` (a header button), `setTheme`, `setTV`, `setXF(field,key)`, `feed(msg)`.
