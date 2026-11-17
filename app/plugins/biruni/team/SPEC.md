# Team rooms through a shared folder (`team/folder.py`, `team/drive_ui.py`)

A room's changes, joins, and code rotations can travel through any shared folder (Google Drive for desktop,
OneDrive, Dropbox, a NAS/SMB share, or a USB stick) without the host and members ever being online at the same
time.

## Layout

`<parent>/biruni-room-<tag16>/`

- `room-<epoch:04d>.bpr` — write-once marker, sealed with the room key (`biruni-room-marker-1`) and signed by
  the host. Stops silent re-creation when a folder is deleted or binned.
- `d-<member id>/` — one subfolder per computer, written by that computer only:
  - `head-<n:06d>.bph` — write-once, sealed and signed index of `{name: sha256}` and host `from` sequence.
  - `snap-<hi:08d>-<part:02d>.bpf` — write-once snapshot of the last `DOWN_KEEP` (20,000) entries.
  - `<seq:06d>-<sha12>.bpf` — write-once envelopes of pigeon packs (`up` from members, `down` from host).
  - `join-<n:06d>.bpj` — sealed, signed join (or `rebase`) request written by a joining/behind computer.
  - `base-<pkg_id:12>-<i:04d>.bpb` — 8 MiB chunks of a vectors-only zip base package (at most 2 GB total, extra
    vector layers capped at 100 MB, rasters omitted), sealed with a random `pkg_key`.
  - `welcome-<joiner16>-<n:04d>.bpw` — `box_seal`ed to the joiner's X25519 `xpub` and signed by the host,
    carrying `pkg_key` and `room_info` (or `"refused"`).
  - `rekey-<at>.bpk` — sealed with the old room key (`biruni-room-rekey-1`) and signed by the host, carrying
    `box_seal`ed new-code boxes for active members; kept 90 days.

## Security

- **Folder access without the code:** sizes, times and ids only. It can delete, hold back or replay, but cannot
  read, forge, or even ask to join.
- **Code and folder access, but not approved:**
  - it can read the marker, the join requests (names and public keys), the heads and the stream packs, up to the
    last 20,000 changes;
  - it cannot read the base data, whose key is sealed only to approved members' keys;
  - it cannot write, because it is not in the roster.
- So approval protects the base data, and later changes are protected by the code alone. Give the code only to
  people you would approve.
- A removed member keeps what it already received. Change the code, and unshare the folder in Drive.
- The host right is checked by every computer against the owner's signature (`rules.may_host`). A computer that
  makes itself host without it is followed by nobody.

## Data-entry right and locked fields (`rules.py`, `hub.py`, `node.py`)

- **Data-entry right (`data`):** Members with `"data"` on a layer can edit attributes of existing features only.
  `Hub.decide_op` refuses adds (`cur is None`), deletes (`kind == "del"`), and geometry changes (compared against
  the host's current `cur` via `features.same_geometry`, never against `op["base"]`), pins `result["g"] = cur["g"]`,
  and skips shared-node propagation. Schema changes (`kind == "field"`) from any non-host member are refused
  regardless of right (`"only the host changes fields"`). On the member's computer, `_shape_guard` undoes
  geometry/add/delete edits in the edit buffer.
- **Locked fields (`field_locks`):** Stored in `ROOM_SETTINGS` as `{layer_key: {"doc", "sig"}}` signed by a rank-0
  owner (`rules.make_field_lock` / `rules.locked_fields`). `Hub.decide_op` (for rank != 0, including a host by
  host-grant) refuses any change to a locked field on an existing feature (`"locked field: <name>"`), and on a new
  feature clears locked fields to `None` unless empty or within the author's own signed number range
  (`rules.in_ranges`). `Hub.local` reverts changed locked fields when a rank != 0 host edits locally.
  `_apply_field_locks` sets locked fields read-only in the layer's edit form config for rank != 0.
- **Limits:**
  - The read-only form and the shape guard are a courtesy, and the host's decision is the guarantee.
  - Only the owner, while hosting, sets locks.
  - Renaming a locked field drops its lock.
