# Signed portal (بوابة موقّعة بلا سيرفر)

The portal part of ArcGIS Enterprise (people, groups, items, sharing, search), with **no server**: a portal is a
folder, signed by its owner, that any static host serves. Free, no account, no licence.

## 1. The folder

```
<portal>/portal.json        {"v":1, "index": "<index text>", "sig": "<b64url ed25519 of the index text>"}
<portal>/portal.js          window.BIRUNI_PORTAL = <portal.json>   (file:// pages cannot fetch())
<portal>/index.html         the viewer (page.py) - same file for every portal
<portal>/items/<id>/file.<ext>   a public item, as it is
<portal>/items/<id>/data.bin     a sealed item (shared with groups)
<portal>/items/<id>/thumb.png    a public item's thumbnail (optional)
```

What is signed is the **stored text** (sorted keys, no spaces, UTF-8, integers only), so the browser verifies
the exact bytes without re-creating any canonical JSON.

### Index

```
{v, kind:"biruni-portal", id, name, about, owner:{name, pub (ed25519 hex), box (x25519 hex)}, created, updated,
 seq,                                   # +1 on each save; the viewer warns when served an older one (rollback)
 groups:[{id, name, ver, members:[{name, box, owner?, env:{e, c}}]}],
 items:[{m:"<manifest text>", s:"<sig of m>", keys:{<group id>:{v:<group ver>, w:"<wrapped CEK>"}}}],
 follows:[{url, pub, name}]}           # in the signed index, so the follow list itself cannot be altered
```

### Item manifest (signed on its own: an item can be copied and checked alone)

```
{v, portal, id, type: file|dashboard|mapservice|link, title, desc, tags, owner (pub hex), created, expires|null,
 scope, groups:[...] ([] = public), file:{name, size, sha256 (of the stored bytes), enc, path, mime}|null,
 url|null, thumb|null, rotate}
```

A link shared with groups is stored as a sealed `link.url` file (mime `text/uri-list`), so its address is hidden
too. Map-service and dashboard items are links (a relative path to a map-service folder works from the same host).

## 2. Who can change what

Only the holder of the portal key (this Windows user's key, `biruni/portal/key`, kept encrypted in the settings
like the dashboard key) can change a portal: every change re-signs the index. A copy of the folder elsewhere is
still checkable; an edited copy shows **«التوقيع غير صالح»**.

Identity is a key, not an account: the badge reads «موقّعة من <name> — بصمة XXXX-XXXX». The name is what the owner
wrote; **the fingerprint is the identity**. A link `…#k=<owner key>` pins the key: any other key is refused even
with a valid signature («ليست صاحبة المفتاح المثبّت»). Fingerprints are checked by phone or in person, once.

## 3. Groups are keys

- Each group has a random 256-bit **group key** (version `ver`).
- Each member has an X25519 key; the group key reaches them in an **envelope**: X25519 (ephemeral) → HKDF-SHA256
  (salt = eph ‖ member key, info `biruni-portal-group`) → AES-256-GCM, aad `group:<portal>:<group>:<ver>`.
  Adding a member = one envelope. The owner is a member of every group (that is how the owner reopens keys).
- A shared item is sealed with its own **content key** (AES-256-GCM, aad `item:<id>`), wrapped once per group with
  the group key (aad `cek:<item>:<group>:<ver>`).
- **Removing a member** replaces the group key (ver+1), sends new envelopes to the others, and by default seals
  each of the group's files again with a new content key. Honest limit: **what a member already downloaded or
  decrypted stays with them** — no system, with or without a server, can take that back.
- Member keys: a Biruni user's member key is derived from their portal key ("My key" tab: card `BPM1.<b64url>`
  as text, file or QR). Someone without Biruni gets a **key file** made by the owner, protected by a passphrase
  (PBKDF2-SHA256, 600 000 rounds, AES-256-GCM — PBKDF2 because every browser has it), handed over with the
  passphrase said separately.
- Titles, tags, type, size and dates of sealed items are **visible to all** (the catalogue is public); only the
  content (and a sealed link's address) is hidden.

## 4. Expiry and scope

`expires` and `scope` are inside the signed manifest: they cannot be changed without the owner's key. The viewer
refuses to open an expired item and shows the scope ("for review only…") on the item page.

Honest limit: **expiry in the browser is advisory** — someone who kept the bytes and the key can still decrypt
them later. That is why an item can be marked **«تدوير تلقائي»** (`rotate`): `Portal.sweep()` (button "Sweep
expired") takes expired marked items out of the portal (file and wrapped keys) and replaces the keys of their
groups, so nothing published later opens with the old keys.

## 5. Following portals

`follows` (in the signed index) lists other portals' addresses with their owner key **pinned** by the follower.
The viewer (and `core.aggregate()`) fetches each followed portal's `portal.json`, checks it against the pinned key,
and merges its items, each marked with its origin. A followed portal with a bad signature, another key, or
altered items is shown **untrusted** (red, striped, cannot be opened), never as trusted. A ministry follows its
municipalities; a municipality follows its offices — no central server, no upload.

Cross-host following needs the followed host to allow cross-origin reads (GitHub Pages does).

## 6. The viewer

One static `index.html` (no library, no server): search, filters (type, tag, group, "my groups", public), the
followed portals strip, item page (scope, dates, SHA-256, groups, origin), download with the SHA-256 checked,
"open with my key" (key file + passphrase; kept in this browser's session, or remembered on request), Arabic/English,
RTL. Signatures: WebCrypto Ed25519 where present, else BigInt arithmetic (RFC 8032); X25519: WebCrypto where
present, else a Montgomery ladder (RFC 7748). Both fallbacks are tested.

- **file://** (a USB stick, a shared network folder opened as a file): the index comes from `portal.js`; when the
  browser cannot read an item file, it asks the user to pick it from the folder, then checks its SHA-256.
- **https** (GitHub Pages) and `localhost`: everything works.
- **plain http on a LAN**: browsers give no WebCrypto there; the viewer says so and opens nothing (it cannot check).

`build/bridge-page/portal/index.html` (page.build_bridge_viewer) is the same viewer hosted once:
`https://<user>.github.io/<repo>/portal/#u=<portal folder URL>&k=<owner key>` opens any portal anywhere.

## 7. API (core.py)

`Portal.create(folder, name, owner_name)`, `Portal.open(folder)`, `add_file(path|data, …, groups, expires_days,
scope, rotate)`, `add_link(url, title, kind)`, `remove_item`, `read(item)`, `add_group`, `remove_group`,
`add_member(gid, card=…)`, `remove_member(gid, box)`, `rotate_group`, `sweep()`, `follow(url, pub)`, `unfollow`,
`publish(dest)`, `address(base_url)`; reading: `read_doc`, `verify_doc(doc, pinned)`, `aggregate(index, fetch)`,
`open_item(folder, index, item, priv)`. Keys: `my_card(name)`, `my_key_file(name, passphrase)`.
UI: `portal.open_dialog(plugin)`.
