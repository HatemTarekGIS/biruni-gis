# Team vault (`team_vault/`)

Snapshots of a project's files kept on the computers of the team room, k of n (default 3 of 5). No computer
can read what it keeps. Any k shards give a snapshot back. Lost shards are rebuilt from k of them.

This is not `vault/`, which is the data catalog («الخزانة»).

## Pieces

| File | What it does |
|---|---|
| `rs.py` | Reed-Solomon over GF(256) (poly 0x11d), written here and vectorised with numpy. Systematic `[I ; Cauchy]`, so any k of n rows invert. `encode` / `decode` / `rebuild` (the exact lost shards). |
| `shamir.py` | Shamir over GF(256), byte by byte. `split` / `combine` / `extend` (rebuilds a lost share without the secret being needed). |
| `store.py` | Content-defined chunking: a 64-byte rolling gear sum (numpy, per 8 MiB block), cut on the low 18 bits, chunks 64 KiB – 1 MiB (about 340 KiB on average). Chunk id is BLAKE2b-256. `ChunkStore` keeps this computer's chunks, deflated, once each. |
| `core.py` | Packs and shard files, `Vault` (owner side `own/`, holder side `held/`), restore, folders, recovery kit, `survives()`. |
| `net.py` | The room: the `Board` on the host, a `Keeper` thread on every computer, `HostLink` / `MemberLink`, `on_room`. |
| `ui.py` | The window: Snapshots (health, restore, repair, save to folders, remove), This Computer (keep shards, space), Recovery (kit). |
| `__init__.py` | The API: `snapshot()`, `restore()`, `health()`, `repair()`, `remove()`, `open_dialog()`, `on_room()`. |

## A snapshot

1. The files are cut into chunks. A chunk that an earlier snapshot already sent out is only referenced (`sent` table).
2. The new chunks and the manifest form a **pack**. The manifest holds the files, their chunk lists and SHA-256, and `where` (chunk → pack, offset, length).
3. The pack is sealed with AES-256-GCM (deflated first) under `key = HKDF(secret ‖ password_key, salt=pack id)`. `secret` is 32 random bytes. `password_key` is Argon2id/scrypt of the vault password, or empty.
4. The ciphertext is cut into n Reed-Solomon shards. `secret` is cut into n Shamir shares, and shard i carries share x = i + 1.
5. The public header is signed with ed25519: `{v, pack, owner, k, n, ts, deps, size, ss, h[n], kdf?}`. Here `h[i] = SHA-256(share_i ‖ shard_i)`.

The vault key is derived from the dashboard key: `HKDF(dashboard seed, "biruni-team-vault", "owner key")`. The recovery kit (`BVK1.<seed>.<check>`) carries only this derived key, never the dashboard key.

## Shard file `*.bvs`

`b"BVS1" | u32 len | JSON {"hdr", "sig", "idx", "share"} | shard bytes`

A shard is checked before it is kept and before it is used: signature, index, size and `h[idx]`.

## Who can read

| Holding | What it opens |
|---|---|
| One shard, or up to k-1 | Nothing (Reed-Solomon pieces of ciphertext, and too few Shamir shares). |
| k shards, no vault password | The snapshot. Say so to the user: k members together could open it. |
| k shards, with a vault password | Nothing without the password. A forgotten password cannot be recovered. |

## The room (`vault_*` requests, routed by `team/hub.py`)

The board is a post office that keeps a mailbox per member and what each member reports it keeps. It never sees clear data and cannot forge requests.

| Request | Body | Answer |
|---|---|---|
| `vault_send` | `{items: [{to, kind, meta, data(b64)}]}` | `{sent, full}`. The board holds at most 160 MiB, so the sender retries the rest. |
| `vault_recv` | `{max}` | `{items, more}`. At most 3 MiB per answer. |
| `vault_have` | `{inv: {pack: {o, i[]}}, free, quota, hold}` | `{}` |
| `vault_status` | `{}` | `{me, live, members: {id: {name, seen, age, inv, free, quota, hold}}}` |

Mail kinds:

- `shard`: an owner places a shard on a holder. It travels in 768 KiB pieces. The holder checks it, keeps it within its space and answers `ack`.
- `ack`: `{pack, idx, ok, why}`.
- `ask`: a request signed by the owner, `{kind, packs, reply_to, owner, ts, nonce}`, valid for 15 minutes and never replayed. Holders answer with their shards of those packs as `give`.
- `give`: shard pieces sent back to the owner.
- `drop`: a signed request to delete shards (only the owner's own shards). Nothing is deleted at once: each member
  writes `drop.json` beside the shards and deletes them only after `DROP_GRACE` (30 days). Until then the shards
  still restore, the member reports them with their deletion date (`d` in `vault_have`), and the owner's dialog
  warns and offers Cancel Deletion. Why: a stolen or ransomed owner key (or malware on the owner's PC) could
  otherwise wipe every copy in one stroke. Limit: someone who holds the owner's key for more than 30 days while
  nobody looks can still delete; the space given to the team stays taken 30 days longer.
- `keep`: a signed request that cancels a pending `drop`.

Placement: each shard goes to the online, keeping member that holds the fewest shards of that pack, then the least loaded one. The owner's own computer is skipped. A shard with no acknowledgement after 180 s is placed again.

Health: counted from the members' own reports. `can_lose` is how many of the worst-placed computers can go with the pack still readable. A snapshot's health is the worst of its packs (its own and its `deps`). States are `safe` (can lose ≥ 1), `at risk` (0), `lost` (< k), `waiting` (shards not yet placed).

Repair: gather k shards of every pack that has missing indices, rebuild the missing ones byte for byte (they still match the signed `h`), and place them. On a new PC the packs are learnt from the members' reports and adopted.

Remove: a pack that a later snapshot reuses (its `deps`) stays. Only packs no other snapshot needs are withdrawn, and their chunks stop being offered to new snapshots.

## Without a room

`export_folders` writes shard j of each pack into folder j (USB sticks). Restore from folders scans `*.bvs` and needs any k folders. No signature by the owner is needed to read them, but the password still applies.

## Settings

| Key | Meaning |
|---|---|
| `biruni/team_vault/hold` | Keep shards for the team (default on). |
| `biruni/team_vault/quota_mb` | Space given to the team (default 1024). |
| `biruni/team_vault/kit_seed` | A recovery kit used on this PC (protected). |

Data lives in `<profile>/biruni/team_vault/{own,held}`.

## Tests

`tests/test_team_vault.py`
