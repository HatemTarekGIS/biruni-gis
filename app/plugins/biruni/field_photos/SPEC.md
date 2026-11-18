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
  - The phone's storage holds its own positions in plaintext until they are acked, or for 48 h.
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
