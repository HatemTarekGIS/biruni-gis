# field_photos — photos from the team's phones to this PC

The phone opens a static web page (served locally over Wi-Fi/hotspot or from `https://photos.biruni-gis.com/p/` via `bridge.py`; the old `https://biruni-gis.com/p/` forwards there), takes photos with GPS or indoor room coordinates, compass direction, kind, note and form answers, and sends them to the office PC.

## How photos travel

1. **Direct link (same Wi-Fi / hotspot, or direct WebRTC over the bridge):**
   - The full original photo is sent directly between the phone and the office PC.
2. **Teammate relay (`via` / `handover`):**
   - A teammate's phone that can reach the PC (or meets the phone offline via two QR codes) carries photos sealed for the office PC (`handover.py`).
3. **Encrypted relay fallback (when mobile-data CGNAT/NAT blocks direct WebRTC):**
   - If the direct WebRTC channel does not open within the connection deadline (`10 s`), the page drops the stalled attempt, shows the relay-mode notice, and sends a small **encrypted preview** (in chunks of at most 24 KB to at most 2 healthy Nostr relays as ephemeral events `kind: 25050`, falling back to `ntfy.sh` if Nostr relays refuse or time out).
   - The office PC saves the preview with `"preview": True` under the photo's `photo_id` and returns a signed `preview` receipt.
   - The **encrypted full-resolution original stays on the phone** (`prevSent = true`) and is never deleted until a direct connection (Wi-Fi or direct WebRTC) delivers the original and returns a signed `original` receipt, replacing the preview file in the same record.
4. **Google Drive (per project, off by default; `drive_page.py`, `drive.py`):**
   - The office ticks *Receive photos through Google Drive* for the project. Biruni makes one folder `biruni-photos-<tag16>` in the Google Drive the PC syncs with Google Drive for desktop (`tag = SHA-256("biruni-drive-room-1:" + k)[:16]`). The office shares it with each surveyor's Google account as **Editor**; the surveyor's account may differ from the office's.
   - The phone links Google Drive on a separate page, `https://drive.biruni-gis.com/link/`. Google sign-in and Google Picker run only there. The phone page loads no Google script; it receives a `drive.file` access token by `postMessage`, with the origin checked both ways. The surveyor picks the office folder once with Picker, and the phone checks its name, that it is not in the bin, and that it can add files (`canAddChildren`).
   - Once linked, **every full original goes through Drive**, even when the direct line is open. The line then carries only the small preview at once, so the photo shows on the map. When the computer has the original from Drive, it replaces the preview in the same record. The line carries the original only when Drive cannot: not linked, no folder, Drive full, or Drive failing for 2 minutes since the line opened.
   - Each photo is one file, `p-<device>/NNNNNN-<sha12>.bfp`: `"BFP1" | header length | header JSON | ECDSA signature | sealed photo`. The sealed photo is the same ECDH P-256 + AES-256-GCM envelope used for waiting photos. The header is signed with a per-phone ECDSA P-256 key (not extractable), certified by `HMAC(Ka, "BFP-CERT1|" + pub + "|" + dev)`.
   - The PC reads the synced folder every 15 s (`DriveIntake`), checks the certificate and signature, opens the photo with the handover key, and saves it with `route: "drive"`.
   - **Size:** the phone keeps every photo at 5 MB at most. A larger camera photo is saved again as JPEG, first only a little more compressed, then smaller, keeping the camera's EXIF with Orientation reset to 1. One multipart upload carries it; there are no resumable uploads.
   - **Retries:** a failed upload waits 2, 4, 8 … 64 s, plus up to 1 s of jitter. Google's `Retry-After` wins when longer, up to 10 minutes. `429`, `5xx`, rate-limit `403` and network errors are retried. A lost answer is checked with an `appProperties` search before uploading again, so a photo is never stored twice. An ended token shows *Send photos (n)*: a new token needs one tap.
   - **Cleanup (office choice, per project, `/field-photos/drive-clean`):** `keep` (default), `manual` or `auto`. The files belong to the surveyor's account (`drive.file`), so only the phone can delete them. Over the line, the phone asks `{t:'dhave', nonce, ids}`. The PC answers `{t:'dgot', nonce, clean, ids}`, naming only the photos it holds as full originals, signed `BRDG1|<nonce>|<clean>|<ids>` with its identity key; with no pin there are no receipts. The phone then moves those files to the surveyor's Drive bin (restorable until Google empties it after 30 days), either at once (`auto`) or when the surveyor taps *Free space in Google Drive (n)* (`manual`).

## What is encrypted and what third parties see

- **End-to-end encryption from phone to office PC:**
  - Waiting photos (both the full original and the preview) are sealed in the phone's browser before being stored in IndexedDB (`biruni-photos`) using WebCrypto **ECDH P-256** with the office PC's public key (`handover-p256.key`, whose private key stays on the PC under Windows DPAPI) and **AES-256-GCM**.
  - Messages carried over public relays are additionally sealed with **AES-256-GCM** under the bridge key (`#k=`, kept after `#` so the static web host never receives it).
  - The office PC signs `answer`, `rhello` and every photo receipt (`BRAK1|<photo_id>|<preview|original>|<nonce>`) with its **ECDSA P-256** identity key, verified on the phone against the link pin (`&i=`). A replayed ack or a preview ack cannot cause the phone to delete an original photo.
- **What public relays (`Nostr` and `ntfy.sh`) can see:**
  - Relays cannot read the photo, coordinates, notes or form answers, and cannot forge a receipt.
  - Relays **do see** the meeting topic hash, client IP addresses, ciphertext sizes and message timestamps.
- **What the MQTT broker and Cloudflare tunnel can see:**
  - The MQTT broker, and any `#` subscriber, see the topic (a hash), the sizes, the times and both IPs.
  - Cloudflare's tunnel sees the same, plus the tunnel's hostname.
- **Cloudflare, and Hatem's account (Biruni relay):** topic hashes, IPs, counts and sizes; sealed bytes only.
- **Google (Drive GPS mailbox):** the account, the file names (seq ranges, device folder), sizes and times; sealed bytes only.
- **Phone storage for positions:**
  - The phone's storage holds its own positions in plaintext until they are acked, or for 10 days (a long weekend or holiday with the office shut).
- **What Google sees (Drive route):** file names (`NNNNNN-<sha12>.bfp`), folder names, sizes, upload times, the surveyor's and the office's Google accounts, and the `appProperties` photo id. It cannot read the photo, place, notes or answers. The link page asks for `drive.file` only, so Biruni can touch only the files and folders it made or the surveyor picked.

## Limits

- **The office PC must be open:** Public relays are never used as a file store. Ephemeral Nostr events (`kind: 25050`) are not persisted on disk. If Biruni GIS on the office PC is closed or offline, encrypted photos stay in the phone's browser storage until the PC is open again.
- **`ntfy.sh` public server limits (per IP address):**
  - 250 messages per day; 60-request burst replenished at 1 request per 5 seconds.
  - Attachments: 2 MB maximum per file, 20 MB active storage per IP (3-hour expiry), 350 MiB daily bandwidth. Multiple phones behind the same mobile carrier CGNAT IP share these per-IP limits.
- **Public Nostr relays:**
  - Default maximum event size is 64 KB (`strfry.conf`) to 128 KB (`nos.lol`, `nostr.mom`). Each chunk is sent to at most 2 healthy relays with pacing (`>= 180 ms` between chunks) and exponential backoff on `OK false` or `NOTICE`.
- **Browser storage on the phone:**
  - In private/incognito browsing mode, closing the page erases waiting photos (the page shows a warning banner when private mode is detected).
  - On iOS Safari, script-writable browser storage may be evicted after 7 days of no user interaction.
- **Data residency:**
  - With Google Drive, the encrypted `.bfp` files are stored on Google's servers, which may be outside the country, until they are cleaned up. The first time Drive is turned on for a project, Biruni says so.
  - When using the bridge or relay fallback, encrypted bytes transit public relays hosted outside the country. Organizations subject to strict national data-residency regulations (such as GEOSA in Saudi Arabia) that prohibit even encrypted transit abroad must use the local Wi-Fi / hotspot mode (`"Phone nearby"`).

## Surveyor Identity, Control Panel, Device Blocking, and Daily Report

- **Choice of QR mode (`shared` vs `per_surveyor`):**
  - **One QR code for all surveyors (`shared`, default):** All phones scan the same team QR code, and each surveyor writes their name once (`#me`, saved in `localStorage` under `biruni-me`) before taking their first photo. Pressing **New code** rotates the team bridge key and cuts off all old phones at once.
  - **Separate QR code per surveyor (`per_surveyor`, `surveyor_keys.json`):** Each surveyor gets their own QR code containing a 128-bit random secret token (`#...&sk=<secret>&sn=<name>`). On the phone, `#me` is pre-filled and locked to that surveyor's name, and `sk` is placed inside `meta` before `sealItem` encrypts it with `PCKEY`. On the office PC, `PhotoStore.save` verifies `sk` against `surveyor_keys.json`, binds `surveyor` and `device_id` (`srv_<id>`) to the PC's verified record (preventing name spoofing), refuses any photo without a valid `sk`, and lets the office revoke or replace a single surveyor's QR code immediately without affecting the rest of the team (cannot be bypassed by clearing browser storage).
- **Surveyor identity inside the sealed envelope:**
  - The phone page requires the surveyor's name (`#me`, saved in `localStorage` under `biruni-me`) before the first photo is taken.
  - Each phone browser generates a persistent random `device_id` (`DEVID`, saved in `localStorage` under `biruni-dev`).
  - Both `surveyor` and `device_id` are placed inside `meta` **before** `sealItem` encrypts the record with the computer's public key (`ECDH P-256 + AES-256-GCM`). The outer query parameter `?m=` carries only `{id, sealed: 1, t}`; on the computer, `server.handover.open(data)` replaces `meta` with the decrypted inner dictionary, so outer fields cannot spoof `surveyor` or `device_id`.
  - Earlier photos without a surveyor name keep all their attributes and field order untouched (`FIELDS[:15]` unchanged; `surveyor` and `device_id` appended at the end) and are labelled `Unknown` (`غير معروف` in Arabic).
- **Surveyors panel and controls (`field_photos/dock.py`):**
  - Lists every surveyor/device with: Name, State (`Connected` / `Offline` / `Blocked`), Network, Link quality, Average GPS accuracy of their photos (`± X m`), Battery, Connection route (`Direct` / `Relay` / `Teammate` / `Local network`), Today's photo count, Total photo count, Previews waiting for full originals, and Last seen time.
  - **Controls:**
    - **Show only their photos / Show all:** applies a non-destructive `setSubsetString` filter on the `field_photos` layer without deleting any features.
    - **Today's work:** filters by today's date (for one surveyor or all).
    - **Zoom to their work:** zooms the map canvas to the bounding box of that surveyor's photos.
    - **Export their work…:** exports that surveyor's features to `.gpkg` or `.csv`/`.xlsx` alongside a `<name>_photos/` folder containing copies of their photo files.
    - **Unify name…:** unifies different spellings of a surveyor's name across `field_photos.gpkg`, `photos.jsonl`, and the live table.
    - **Block device…:** records `device_id` and the reason in `blocked_devices.json`. Future photo uploads from that `device_id` are refused with HTTP `403` and logged with timestamp and reason in `rejected.jsonl`, while the device's existing photos remain untouched.
  - **Daily report (`Daily report…`):** writes an HTML or PDF summary for the day showing each surveyor's photo count today, total photos, first and last photo time, previews waiting for originals, and average GPS accuracy.

## Traffic and relay pool

- **Send-on-change dead reckoning (`ladder_page.py` `DR_JS`, `gps/deadreckon.py`):**
  - In GPS mode (unless the PC opens the link with *Full (every position)*, `&f=1`), fixes are filtered on the phone against flat-earth extrapolation (`111320 m/deg`, longitude scaled by `cos lat`) capped at `EXTRAP_MS = 10 s` (and only while moving with finite course).
  - **Thresholds (`DRC`):** a fix triggers a send when `force` is set, on the first fix, after `HEARTBEAT_MS = 30 s`, when distance from the predicted position exceeds `max(5 m, 2 * min(acc, 50 m))`, when moving and course changes by more than `TURN_DEG = 25°`, or when moving state flips (`moving >= 0.8 m/s`, `stopped <= 0.4 m/s`). When distance, turn, or flip triggers and `skipped.t > last.t`, `[skipped, p]` is sent to preserve corners.
  - **PC marker interpolation:** `tracking.py` runs a `500 ms` `QTimer` that moves only the map marker via `deadreckon.predict` from the last valid non-late fix while speed `>= 0.8 m/s` and course is known (elapsed time capped at `10 s`), leaving the recorder and value labels untouched.
- **Batching and stickiness (`ladder_page.py`, `bridge.py`):**
  - Relay channels (`own`, `mqtt`, `nostr`, `ntfy`) batch positions for `BATCH_MS = 10 s` while backlog `<= 60`, reverting to `minGap` (`2 s` / `3 s`) when backlog `> 60` so catch-up drains at full pace. `direct` (`900 ms`) and `tunnel` (`1 s`) keep `minGap`.
  - `LAD.primary()` holds for `PRIMARY_TTL_MS = 90 s` (still falling back after a `3 s` stall), avoiding multi-channel races during sparse sending. Higher relay channels are probed at most once per `PROBE_RELAY_MS = 60 s` and only on ticks that also send to the primary; `direct` and `tunnel` probes stay at `15 s`.
  - `SIG.sendRaw` sends on one healthy relay; `RECV_JS` replies with `SIG.sendOn(relay, ack)` on the relay that delivered the envelope (falling back to `sendRaw`). `PHONE_JS` `later()` caps reconnect backoff at `300 s` while `LAD.primary()` exists (`60 s` otherwise).
- **Relay pool, per-room subsets, and legacy anchors (`relay_pool.py`, `bridge.py`):**
  - Pools: `NOSTR` (5 legacy + 3 additional relays) and `MQTT` (3 legacy brokers), ranked per room topic by `SHA-256(topic + "|" + url)` hex descending (`K = nostr: {phone: 3, host: 5, scan: 8}, mqtt: {phone: 2, host: 3, scan: 5}`).
  - **Subset rule:** the PC host (`127.0.0.1`) connects to the top `host` relays of the pool plus the top-ranked legacy relay per kind (**legacy anchor**, guaranteeing overlap with older un-updated pages). Viewers with `&p=2` connect to the top `phone` relays (a subset of `host`); viewers without `p=2` connect to all legacy relays.
  - A `4 s` health check closes and replaces any pooled member not up `8 s` after opening or with `fails >= 2` with the next ranked URL down to `scan`.
  - **Release order:** build and publish the updated static phone/viewer pages (`p/`, `d/`, `s/`) before releasing the desktop app update that adds `&p=2` to links and enables the expanded pool.

## Your organization's relay

- **One-click Cloudflare Worker (`org_relay.py`, `org_relay_dialog.py`, `org_relay_worker.js`):**
  - Organizations can deploy their own unbudgeted copy of `biruni-relay` (bundled from `relay/hub.js` and `relay/worker.js`, with no `ROOM_PER_HOUR` or `DAY_BUDGET` vars) directly from Biruni's **My organization's relay…** dialog (in the GPS panel's bridge page and Field Photos' bridge mode).
  - The user pastes a scoped Cloudflare API token (`Account · Workers Scripts · Edit` and `Account · Account Settings · Read`). The field uses password echo mode, is cleared as soon as Deploy starts, and the token stays strictly in memory inside the worker's closure — never written to disk, `QgsSettings`, logs, the activity log, or `OrgRelayError` text (where any occurrence of the token is scrubbed to `***`). Network calls use `urllib.request` directly (never `QgsNetworkAccessManager`).
  - Only the resulting validated hostname (`ORG_RE`: `^[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.workers\.dev$`) is stored in `QgsSettings` under `biruni/bridge/own-relay`, appended to links as `&o=<host>` after `&p=2`, and also sent over the bridge via `hello`/`rhello` and signed `{t:'org', h}` (`BROR1|<h>`).
  - **CSP justification:** `bridge.with_csp` adds `wss://*.workers.dev` to `connect-src` for the phone photos page only (not `d/` or `s/`). Every payload sent over the organization's relay is sealed end-to-end with AES-GCM under the room key (and photos additionally with the office PC's handover key), while `script-src` remains strictly hash-only (`'sha256-...'`).

## Biruni's relay is the last resort

- **Fair-use limits on Biruni's shared relay (`relay/hub.js`, `relay/worker.js`, `relay/wrangler.toml`):**
  - `ROOM_PER_HOUR = "600"`: each room has a token bucket of capacity `600` refilled at `600` messages/hour; exhausting it blocks that room for `10 minutes` (`600000 ms`) and closes messages and new joins with `1013`.
  - `DAY_BUDGET = "1600000"`: counts accepted messages per UTC day across the hub; above the budget, every message and join closes with `1013` until the UTC day changes. State is memory-only (no storage writes, no logging, no per-IP state).
- **Client priority and lazy own relay (`ladder_page.py`, `bridge.py`):**
  - The fallback ladder order is: `direct`, `tunnel`, `org`, `mqtt`, `nostr`, `ntfy`, `own`, `drive`; `SIG.healthyAck()` orders by `org`, `mqtt`, `nostr`, `own`, then by `fails`.
  - On a viewer (`!HOSTSIDE`), `OwnRelay` for Biruni's shared relay (`BR.own`) opens lazily only after `BR.ownLazyMs` (`10000 ms`) without any message from the PC, and closes once `direct`, `tunnel`, or `org` has been the primary for `60 s`. The desktop host keeps `own` open, and `OwnRelay` pings every `55 s` for `own` (`25 s` for `org`).

## Capture schema

- **Templates and the office tree (`capture.py`, `capture_templates.py`, `capture_dialog.py`):**
  - A capture schema defines a dynamic tree of what the field surveyor must photograph and fill in (`{"biruni_capture": 1, "id", "rev", "title", "job": [<questions>], "nodes": [...]}`).
  - `clean_schema` limits a template to at most `48 KB` (`MAX_SCHEMA_BYTES = 48000`), `200` nodes (`MAX_NODES = 200`), and depth `6` (`MAX_DEPTH = 6`).
  - Each node has `id` (`^[a-z][a-z0-9_]{0,23}$`, unique across the schema), `label` (`<= 60` chars), `req` (default `True`), optional `photos: [min, max]` (`0 <= min <= max <= 50`), optional `repeat: {"min", "max"}` (`max <= 200`), optional node questions `q` (`clean_questions`, with `"title": True` marking the question that names a repeating instance), and child `nodes`.
  - Four built-in templates ship in `capture_templates.py` (`building`, `villa`, `vacant_land`, `shop`), and custom or edited templates are saved in `<qgisSettingsDir>/biruni/capture_schemas/<id>.json` via `SchemaDialog`.
- **Jobs and items (`capture_jobs.py`):**
  - `JobStore` stores each job under `<photo folder>/jobs/<jobId>/job.json` (`{"biruni_job": 1, "id", "title", "schema": "<id>@<rev>", "schema_snapshot": {...}, "data": {...}, "loc": [lon, lat] | None, "to": "<surveyor>", "status": "open"|"closed", "created", "closed"}`).
  - Each job freezes its own `schema_snapshot` at creation so later edits to the template do not alter existing jobs.
  - Each checklist slot uses key `k = "<iid>:<nodeId>"`, where `iid` is the 6-character random ID of the nearest repeating-ancestor instance (`""` at the root), or `"*"` for an extra photo of the job.
  - Photos taken inside a job carry `meta["cap"] = {"j": jobId, "k": slotKey, "pl": pathLabel, "ch": [[nodeId, iid, order], ...]}` inside the sealed envelope. On the PC, `PhotoStore.save` stores `cap` in `photos.jsonl`, and `dock.py` writes `cap["j"]` and `cap["k"]` into the `job_id` and `job_item` columns of `field_photos.gpkg`.
- **`guide.cap` (`capture.py`, `server.py`):**
  - `PhotoServer._publish_guide()` attaches `guide["cap"] = {"v": 1, "schemas": {"<id>@<rev>": {...}}, "jobs": [{"id", "title", "s": "<id>@<rev>", "data", "loc", "to", "st": "open"}]}` whenever open jobs exist (omitting `cap` completely when there are no open jobs).
  - `cap_payload` enforces `MAX_CAP = 60000` bytes. The phone caches `guide.cap` separately in `localStorage` under `biruni-cap-<ROOM>`.
- **Phone checklist and state items (`BCS1`, `server.py`, `drive_page.py`):**
  - When `guide.cap` is present, `#capCard` shows the job picker, `{done} of {total}` progress bar, and checklist tree (`✓`, `✗`, `⏭`), while `#capSheet` renders the item path, node questions, note, `"Take a photo (have/min)"`, and `"Cannot do this item"` with a skip reason (`>= 3` characters).
  - Job state lives in `localStorage` under `biruni-job-<ROOM>-<jobId>` (`{"v": 1, "job", "dev", "by", "rev", "t", "s", "inst", "ans", "note", "ph", "skip", "closed"}`) without bumping the IndexedDB `biruni-photos` version (`1`).
  - `capSnap()` (debounced `3 s`) queues a full state snapshot in IndexedDB `q` with `id = "js" + rev36 + "-" + rand9`, `meta.cs = {"j": jobId, "rev": rev}`, `cap: 1`, and plaintext payload `"BCS1" + JSON`, replacing any older unsent state snapshot for the same job. While `lineOpen()` is true, `dfits` excludes state items from Drive upload so live progress does not wait on a Drive sync.
- **Intake and progress (`capture_jobs.py`, `capture_progress.py`, `server.py`):**
  - `PhotoStore.save` recognises state items (`meta` with `cs` and payload starting with `BCS1`) before image `sniff()` and after `_check_sender()`, forwarding them to `JobStore.take_state`.
  - `take_state` keeps the highest `rev` per `(job, dev)` in `jobs/<jobId>/state-<dev>.json` (ignoring older or replayed snapshots). Across devices, `merge_states` takes the union of `inst` (later `del` wins) and `ph`, and uses last-writer-wins by timestamp `t` for `ans`, `note`, and `skip`. If all `state-*.json` files are absent, `tree()` rebuilds the instance tree from the `cap.ch` chains in `photos.jsonl`.
  - `ProgressDialog` shows a 6-column tree (`Path`, `Required`, `Photos (have/min)`, `Answers`, `State`, `On the phone, not here yet`), a `Missing only` filter, double-click feature selection on `field_photos`, and buckets for extra photos (`*`), unbound photos taken during the job, and duplicate instances created offline by two surveyors.
- **Sealed PDF report (`capture_report.py`):**
  - `report_model` and `write_report` generate an A4 PDF (RTL for `ar`/`fa`, LTR otherwise) using `survey/report_pdf.py` (`_Pen`, `_table`, `font_family()`):
    1. Cover and job data;
    2. Location map (`QgsMapRendererCustomPainterJob` over project layers at photo extent + 20%, or a coordinate grid with north arrow);
    3. Statistics (instances per repeating node, choice/yes-no tallies, integer/number sums and averages, photos per section, and missing/skipped items with reasons);
    4. Photo pages (`2×3` grid in schema tree order, decoded one at a time via `QImageReader` scaled to `1000 px` long side, with LRI/PDI-isolated coordinates, framed placeholders for undecodable HEIC files, and `[Preview]` labels);
    5. Appendix of extra photos (`k == "*"`);
    6. Signature and stamp boxes.
  - Every generated report is sealed with `seal.core.seal_file(path, certificate=True)`, writing `.seal.json`, `.seal.png`, and `.seal.html`.

## No-loss program

- **Item states (`st`/`c`/`h`) and the 2-copy rule (`copies.py`, `server.py`, `drive_page.py`, `ladder_page.py`):**
  - Invariant: no item is deleted from the phone or trashed from a cloud mailbox until at least **2 verified copies** exist.
  - Each phone queue record in IndexedDB `q` carries `st` (`0` kept on phone, `1` reached cloud `☁️`, `2` reached office `✓`, `3` protected by 2 copies `✓✓`, `9` refused `✗`), `c` (verified copy count), `h` (hex SHA-256 of the sealed blob), and `at` (last state change timestamp). Items with `st >= 1` are not re-sent; refused items (`st = 9`, e.g. HTTP `403`) stay on the phone with their refusal reason and are never deleted.
  - On the PC, `CopyLedger` maintains `<photo folder>/copies.jsonl` with `{"id", "sha", "ssha", "places": {"office", "mirror", "drive_mb", "r2", "backup", "archive", "gdrive"}}`.
  - The phone deletes an item from `q` only when `CopyLedger.count(id) >= 2` (where `office` + `drive_mb` counts as 2 for the phone delete rule). Single-PC offices configure a **Second copy folder** on another volume (`different_volume`); every file written there is re-hashed from disk before marking `mirror`.
  - A Google Drive `.bfp` mailbox file is released for trashing (`PhotoStore.originals(..., for_drive_clean=True)`) only when `CopyLedger.non_mailbox_count(id) >= 2` (`office`, `mirror`, `backup`, `archive`, `gdrive`). A Drive `.bgp` GPS file is trashed only when `r.hi <= LAD.upto` (never by age alone).
  - At the office's configured end-of-day hour (default `18:00`) and on `visibilitychange` to `hidden`, `#eodSheet` lists any items still at `st < 3` or `st == 9`.
- **Receipts (`BRAK2`, `BRCP1`, `BRLA2`, `BRLT1`; `bridge.py`, `server.py`, `ladder.py`):**
  - `hello`, `rhello`, and `GET /<token>/ping` advertise `nl: 1` (sticky per room in `localStorage` under `biruni-nl-<ROOM>`).
  - **`BRAK2`**: alongside legacy `is` (`BRAK1|<id>|<kind>|<nonce>`), photo acks carry `c`, `h`, `rt`, and `is2` signing `BRAK2|<id>|<kind>|<nonce>|<h>|<c>|<rt>`. The phone checks `h` against its sealed blob SHA-256 before updating `st` and `c`.
  - **`BRCP1`**: the phone polls `{t: "chave", nonce, ids}` (or `rchave` / `POST /copy-status`); the PC replies `{t: "cgot", nonce, rt, items: [[id, c, h8], ...]}` signed `BRCP1|<nonce>|<rt>|<id>:<c>:<h8>,...`.
  - **`BRLA2`**: GPS position acks (`lack`) carry `u2` and `is2` signing `BRLA2|<d>|<e>|<upto>|<u2>` alongside `BRLA1|<d>|<e>|<upto>`.
  - **`BRLT1`**: when an `lpos` envelope fails the 15-minute timestamp window, the PC replies with `{t: "ltime", now}` signed `BRLT1|<now>` (verified on the phone before the timestamp window check so a skewed clock self-heals).
- **Clock skew (`server.py`, `bridge.py`, `ladder.py`):**
  - The phone learns `SKEW` (`server_ms - Date.now()`) from the HTTP `Date` header of a same-origin `HEAD sw.js` probe, `hello.now`, `ack.rt`, and `BRLT1` (`ltime`), and uses `NOW() = Date.now() + SKEW` for Nostr, signaling, and ladder timestamps.
  - Photos sealed before `SKEW_OK` is known carry `"tu": 1` inside the sealed `meta` and an outer `"dt"` correction on delivery; `clean_meta` applies `dt` only when `tu == 1`, clamped to `±30 days`. `Ladder.take` adjusts position timestamps `t` to true PC time when drift exceeds `60 s`.
- **iOS gate and persistent storage (`server.py`, `bridge.py`, `build_bridge_page.py`):**
  - `build_bridge_page.py` writes `p/manifest.webmanifest` with `"display": "standalone"` and no `start_url` so the installed Web App keeps its `#k=` link, and `BOOT_JS` remembers the last valid link in `localStorage` (`biruni-last-link`).
  - On iOS outside standalone mode (`!navigator.standalone`), capture is gated behind `#iosGate` with step-by-step `"Add to Home Screen"` instructions and a **Copy link** button; if the installed PWA opens without `#k=`, `#iosPaste` validates and activates the pasted office link via `startBridge(k)`. Android shows a soft install bar (`#androidBar`) on `beforeinstallprompt`.
  - The page calls `navigator.storage.persist()` (showing `#persistWarn` if refused) and `navigator.storage.estimate()` to display free space (`#spaceInfo`), reserves `15 MB` headroom, and stops capture cleanly on low space or `QuotaExceededError` (`#quotaWarn`).
  - `#unsentWarn` (`"{n} items not protected for {d} days"`) turns amber at `3` days and red at `5` days.
- **`.bfx` bundles (`bundle.py`, `server.py`, `dock.py`):**
  - The phone exports waiting and refused items into sealed `.bfx` files (`<= 200 MB` per chunk) via `navigator.share({files})` or download links without deleting anything from the phone.
  - Layout: `"BFX1" | 4-byte big-endian index length | UTF-8 JSON index | concatenated sealed blobs`. The index is strictly `{"v": 1, "items": [{"id", "size", "sha", "st"}]}` and carries no surveyor names, coordinates, or notes.
  - On the office or backup PC, **Import bundle…** (`bundle.import_bundle`) validates the index and SHA-256 hashes atomically, unseals every item through `PhotoStore.save(..., ssha=sha)`, deduplicates by `id`, and records the copy in `CopyLedger`.
- **Envelope v2 and the recipients list (`BRRL1`; `envelope.py`, `recipients.py`, `handover.py`):**
  - `HandoverKey.open` dispatches on byte `0`: `0x01` is single-recipient envelope v1; `0x02` is multi-recipient envelope v2.
  - **Envelope v2 layout:** `0x02 | n (1 byte, 1..12) | eph (65-byte uncompressed P-256) | n × [kid (8 bytes) | wrap (48 bytes)] | iv (12 bytes) | ct`.
    - `kid = SHA-256("biruni-drive-kid-1:" + pub_b64u)[:8]`.
    - A random 32-byte content key `CK` is wrapped for each recipient `i` with `AES-256-GCM(KEK_i, iv = 0^12, CK)`, where `KEK_i = HKDF-SHA256(ECDH(eph, R_i), salt = eph || R_i, info = "biruni-seal-2")`.
    - The payload `4-byte big-endian meta length | meta JSON | data` is encrypted with `AES-256-GCM(CK, iv, plaintext, aad = header_bytes_before_iv)`.
  - **Recipients list (`BRRL1`):** stored in `<cert folder>/recipients.json` and published in `guide.rl` and `hello.rl` as `{"l": canonical_json, "ip": signer_pub, "is": sig}`, where `l` is `{"v": 1, "seq", "room", "made", "r": [{"k": kid_hex16, "p": pub_b64u, "role": "office"|"backup"|"emergency"|"sup", "scope": ["photo"|"state"|"pos", ...], "n": name}]}`.
  - Signed with ECDSA P-256 by `bridge.identity(key)` over `"BRRL1|" + hex(SHA-256(l))`. `seq` is floored to minutes since 1970 (`next_seq`).
  - The phone refuses any list when the link has no `&i=` pin (no TOFU; pinless links keep sealing v1), when the signer does not match the pin, when `room` differs, when `seq` rolls back for the same office key, when the `office` entry is missing, or when `len(r) > 12`. Once a list is stored (`biruni-rl-<ROOM>`), only a signed list can change the office public key (`rlPk`).
- **Emergency key (`BEK1`; `emergency.py`, `keys_dialogs.py`):**
  - Generates an in-memory P-256 key pair, splits its PKCS#8 DER private key with Shamir 3-of-5 (`team_vault.shamir.split(..., 3, 5)`) into five `biruni-emergency-<x>-<holder>.bek` files, wipes the private key from memory (it never touches disk unsplit), and adds the public key to `recipients.json` with `role: "emergency"`.
  - Each `.bek` share is JSON: `{"biruni": "emergency-share", "v": "BEK1", "pub", "x": 1..5, "k": 3, "of": 5, "holder", "made", "share": b64u, "check": SHA-256("BEK1|" + pub + "|" + x + "|" + share)[:16]}`. Any 3 shares reconstruct the private key in memory (`emergency.recover`) to open `.bfp`, `.bfx`, or v2 envelopes.
- **Key backup v2 (`handover.py`):**
  - `handover.backup_v2` writes `{"biruni": "biruni-handover-key", "v": 2, "pub", "made", "kdf", "ct"}` sealed with Argon2id/scrypt + AES-256-GCM (`aad = "biruni-handover-key-2|" + pub`) under an admin-strength password (`passwords.MIN_ADMIN`), and keeps a timestamped copy in `<cert folder>/keybackup/`.
  - The encrypted `body` holds:
    - `"handover"`: the office P-256 handover private key PEM;
    - `"bridge"`: the Field Photos and GPS bridge keys (`biruni/field-photos/bridge-key`, `biruni/gps/bridge-key`);
    - `"ids"`: the ECDSA P-256 identity private keys (`&i=` pins);
    - `"rl"`: the recipients list (`recipients.load`);
    - `"pl"`: the tracking policy (`policy.load`);
    - `"darchive_pw"`: the Google Drive archive AES-256 ZIP password (`drive_archive_pw.dat`, when present);
    - `"backup_pw"`: the restic repository password (`backup_pw.dat`, when present);
    - `"rclone_pw"`: the rclone configuration password (`rclone_pw.dat`, when present).
  - Restoring a v2 backup on a replacement PC preserves all existing QR codes, `&i=` pins, sequence numbers (`recipients.merge`, `policy.merge`), and archive/backup passwords (keeping any differing local keys aside under `*-before-restore` or `.old`). v1 backups still restore.
- **Backup PC and office pack (`BOP1`; `recipients.py`, `backup_intake.py`, `keys_dialogs.py`):**
  - A backup PC generates its own `HandoverKey`, gives its public key to the primary office PC (added to `recipients.json` as `role: "backup"`), and receives an **office pack** (`BOP1` + envelope v2 sealed exclusively for the backup PC's public key, with `meta = {"biruni": "office-pack", "v": 1, "made", "office": office_kid_hex16, "keys": {...}, "surveyors": {...}}`).
  - After the user confirms the displayed `office` key ID matches the primary office PC, the pack is stored DPAPI-protected in `<cert folder>/office-pack.bin`.
  - The backup PC can open `.bfp` and `.bfx` files manually (**Open sealed files…**) or run `BackupIntake` automatically (`biruni/backup/auto-intake`), which runs a read-only `DriveIntake` and `PosIntake` for every bridge key in the pack (saving photos to its own `PhotoStore` and `.lpos` files under `<photo folder>/positions/`, without ever trashing Drive files or sending receipts).
- **System health, owners, and the runbook (`health/checks.py`, `health/owners.py`, `health/dialog.py`, `health/runbook.py`):**
  - `health.checks.run(ctx)` evaluates registered checks (`ok` 🟢, `warn` 🟡, `bad` 🔴, `na` ⚪; any check raising an exception becomes `na`):
    - `key_backup` (red if missing, `v < 2`, wrong `kid`, or holder left; amber if `> 180` days old or older than `recipients.json`);
    - `second_copy` (red if unset or unreachable; amber if on the same volume as the photos folder);
    - `emergency_key` and `holder:1..5` (amber if absent; red if any share holder left);
    - `surveyor:<dev>` (amber if oldest unsent item `> 3` days or refused items `n9 > 0`; red if `> 5` days);
    - `owner:drive`, `owner:cloudflare`, `owner:domain` (amber if unset or personal webmail via `is_personal(email)`; red if marked in `left`);
    - plus the P9 backup, sync, and off-site rows (`backup/health_rows.py`).
  - `<cert folder>/owners.json` (`{"v": 1, "org", "services": {sid: {"owner", "email", "at"}}, "key_backup": {"holder", "where"}, "left": {name: date}}`) records who manages each service and who left (`Someone left…`). Both the Google Drive and Cloudflare Worker setup flows prompt for the owner's name and organisation email.
  - Four seconds after startup, `check_on_startup` runs all checks and pushes a non-blocking `messageBar` alert at most once per day (`biruni/health/last-alert`).
  - `runbook.write_runbook` prints a single A4 RTL Arabic emergency sheet PDF containing the organisation name, PC name, key backup location and holder, the 5 emergency share holders (`k = 3`), service owners, and recovery steps, with zero secrets or pins.
- **Coordinate-free status heartbeat (`hs`; `server.py`, `bridge.py`, `ladder.py`, `presence.py`):**
  - `hello`, `rhello`, and `GET /<token>/ping` advertise `hs: 1`. After seeing `hs: 1`, the phone sends `hs` every `60 s` and on `visibilitychange` (even when tracking is `off`): `{"d": devId, "n": name, "sk", "q": [n0, n1, n2, n9], "old": oldestPendingMs, "lp": ladderBacklog, "tm": effectiveMode, "cs": consentedSeq, "ps": persisted, "t": NOW()}`.
  - Travels over the direct line (`{t: "hs", h}`), relays (`{t: "rhs", h}`), LAN (`POST /<token>/hs`, capped at `512` bytes), and piggybacked as `env.hs` inside `lpos` (including Drive `.bgp` files).
  - Authenticated through `store.resolve_pos`, `hs` is display-only (never touches `CopyLedger`) and updates `<photo folder>/team_seen.json` via `Presence` (throttled atomic save every `30 s`).
- **Tracking policy (`BRPL1`) and consent (`BCN1`; `policy.py`, `tracking_dialog.py`, `server.py`, `dock.py`, `tracking.py`):**
  - **Policy (`BRPL1`):** stored in `<cert folder>/tracking_policy.json` and carried in `guide.pl`, `hello.pl`, and `rhello.pl` as `{"l": canonical_json, "ip", "is"}`, signed with ECDSA P-256 by `bridge.identity(key)` over `"BRPL1|" + hex(SHA-256(l))`.
  - Wire list `l`: `{"v": 1, "seq", "room", "made", "mode": "off"|"photo"|"15"|"5"|"1"|"live", "hours": [[d, from_min, to_min], ...], "tz": utc_offset_min, "per": {tag16: mode}, "jobs": {jobId: mode}}`, where `tag16 = SHA-256("biruni-pl-sk-1:" + secret)[:16]` so surveyor secrets never appear in the guide.
  - No TOFU: pinless links refuse every policy and keep the manual `share` toggle. Precedence is `per[tag] ?? jobs[CAPJOB] ?? mode`, falling to `off` outside working hours (`NOW() + tz`) or without consent. `m=gps` (`GPSMODE`) is exempt and always runs `live`.
  - **Consent (`BCN1`):** `#consSheet` asks for consent on the first policy, when `RANK` (`off < photo < 15 < 5 < 1 < live`) increases, or when work hours change; declining or withdrawing via `#trkPill` sets tracking to `off` while still allowing photo capture with photo coordinates.
  - Each consent decision queues a state item (`id = "cn-" + DEVID + "-" + seq` for accept, or `"-0"` suffix on withdrawal, `meta.cn = 1`, `cap = 1`) sealed for `rlFor("state")` with payload `"BCN1" + JSON({"v": 1, "dev", "by", "seq", "mode", "hours", "a": 1|0, "t"})`. `PhotoStore.save` appends verified records idempotently to `<photo folder>/consents.jsonl` and marks `CopyLedger`.
  - On the PC map, `team_live` draws active surveyors in blue (`#2B7FFF`) and inactive/silent surveyors (`> 150 s`, or `> 75 s` in `tracking.py`) in grey (`#8A8F98`) with their last-seen timestamp from `Presence`.
- **Office archive (P7) and Google Drive archive (P8) (`archive.py`, `archive_xlsx.py`, `archive_search.py`, `archive_drive.py`):**
  - **Office archive (`archive.py`, `archive_xlsx.py`, `archive_search.py`):** built automatically on `JobStore.close_job` (and via **Build archive**) under `<photo folder>\أرشيف بيروني\<job no> - <title>\` (configurable under `biruni/field-photos/archive-root`, keeping paths `< 240` chars).
    - Creates numbered section folders in schema tree order (`01 <section>\`, …), nested repeating-instance subfolders named by their title answers, and `99 صور إضافية\` for extra photos (`k == "*"`).
    - Copies originals as `<job code>_<item path short>_NN.jpg`, re-hashes each copy from disk against the original SHA-256, and marks `archive` in `CopyLedger`.
    - Writes `<job no>.xlsx` (`Job data`, `Items`, `Photos` with clickable relative hyperlinks and original SHA-256, and `Statistics`), copies or generates `<job no>.pdf` with its `.seal.*` files, and atomically updates `<archive root>\فهرس الأرشيف.xlsx` (searchable via `ArchiveSearchDialog` by client, deed number, district, date range, or surveyor).
  - **Google Drive archive (`archive_drive.py`):** optional per office (`biruni/field-photos/drive-archive*`), writing into a folder synced by Google Drive for desktop in one of three formats:
    - `folder`: open P7 folder tree;
    - `zip`: `<job code>.zip` (DEFLATE);
    - `aes`: `<job code>.encrypted.zip` using WinZip **AE-2** (`0x9901` extra field, AES-256-CTR little-endian counter starting at 1, PBKDF2-HMAC-SHA1 1000 iterations, HMAC-SHA1-80 authentication, `CRC-32 = 0`), with flat code-only entry names (since ZIP headers do not encrypt file names). The random password (`XXXXX-…`) is shown once, stored under DPAPI in `<cert folder>/drive_archive_pw.dat`, and included in key backup v2.
    - **Compression levels:** `original` (byte-identical), `high` (JPEG `q85`, long side `4000 px`, keeping EXIF), and `compact` (JPEG `q70`, long side `2560 px`, keeping EXIF).
    - **CopyLedger rule:** written files are re-hashed from disk (and ZIP/AE-2 archives deep-verified) before recording completion in `drive_archive.json`, and **only the `original` level marks `gdrive` in `CopyLedger`** (recompressed archives never count toward the 2-copy delete rule). A quota meter warns when free space on the Drive volume drops below `10%`.
- **Backups (`backup/tools.py`, `backup/secret.py`, `backup/restic.py`, `backup/engine.py`, `backup/schedule.py`, `backup/syncthing.py`, `backup/offsite.py`, `backup/health_rows.py`):**
  - **Executable pinning (`tools.py`):** pins `restic` (`0.17.3`, bundled or downloaded), `syncthing` (`2.1.7`), and `rclone` (`1.76.0`) by official GitHub release URL, `zip_sha256`, and `exe_sha256`, re-verifying `exe_sha256` before every subprocess spawn in a clean environment with `CREATE_NO_WINDOW`.
  - **`restic` (`restic.py`, `engine.py`, `schedule.py`):** repository password lives under DPAPI in `<cert folder>/backup_pw.dat` (and key backup v2 `backup_pw`), passed only via `RESTIC_PASSWORD` in the child environment. `Scheduler` runs on a 10-minute timer while Biruni is open (default: backup every `24 h`, `check --read-data-subset=k/13` every `7 d`, random-folder `--verify` restore test every `30 d`, and `team_vault` key/list auto-snapshot every `7 d`), deferring cleanly if a USB disk is unplugged. Biruni never schedules `forget` or `prune`; `admin_prune` requires typing the exact organisation name.
  - **What marks `backup` in `CopyLedger`:** after `restic backup`, `Engine.mark_from_dump` streams `restic dump <snapshot> <day folder>` as a tar stream (`tarfile` mode `r|`), hashes each member, and calls `ledger.mark(id, "backup")` **only** when the dumped photo's SHA-256 matches the ledger `sha` **and** the repository lives on a different volume from both the photos folder and the mirror folder (`Engine.volumes_ok()`).
  - **`Syncthing` (`syncthing.py`):** runs `syncthing serve --no-browser --no-upgrade` with its GUI bound to `127.0.0.1:<port>` and locked by a random bcrypt password and DPAPI-stored API key (`syncthing_api.dat`, `syncthing_gui.dat`). Office/branch folders use `sendonly`; the main centre uses `receiveonly` with `staggered` versioning. Syncthing completion updates `state.json` for `sync:<peer>` health rows only and **never marks `backup` in `CopyLedger`**.
  - **`rclone` (`offsite.py`):** runs `rclone copy --immutable <restic repo> <remote>` (refusing `sync`, `delete`, `deletefile`, and `purge`), with its configuration encrypted under `RCLONE_CONFIG_PASS` stored in `<cert folder>/rclone_pw.dat` (and key backup v2 `rclone_pw`) and passed via child environment only.

