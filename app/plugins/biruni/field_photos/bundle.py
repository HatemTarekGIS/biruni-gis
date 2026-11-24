"""Sealed export bundle (.bfx) format and PC import for the Biruni no-loss program (Phase P2).

Layout of a `.bfx` file:
  "BFX1" (4 bytes) | index length (4 bytes, big-endian uint32) | JSON index (UTF-8) | concatenated sealed blobs

Index schema:
  {"v": 1, "items": [{"id": str, "size": int, "sha": str, "st": int}, ...]}

Rules:
  - The JSON index holds ONLY `id`, `size`, `sha` (SHA-256 of the sealed blob) and `st` (phone item state).
    It must NEVER hold surveyor names, coordinates, notes, timestamps or any unencrypted metadata.
  - Each chunk created by `pack()` is at most `max_chunk_bytes` (default 200 MB).
  - `parse()` and `import_bundle()` refuse truncated files, trailing garbage, non-strict index keys,
    size mismatches, SHA-256 mismatches, or tampered sealed blobs.
  - Importing feeds `PhotoStore.save(..., ssha=sha)`, deduplicated by `id`, and records each imported item
    in the P1 `CopyLedger` like any received item.
"""
import hashlib
import hmac
import json
import os
import re

from .server import BlockedDeviceError, ID_RE

MAGIC = b"BFX1"
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_CHUNK_BYTES = 200 * 1024 * 1024
MAX_ITEM_BYTES = 40 * 1024 * 1024 + 65536
ALLOWED_INDEX_KEYS = {"v", "items"}
ALLOWED_ITEM_KEYS = {"id", "size", "sha", "st"}
ALLOWED_STATES = {0, 1, 2, 3, 9}
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class BfxError(ValueError):
    """Raised when a .bfx bundle is malformed, truncated, or tampered with."""


def pack_one(items):
    """Pack a sequence of `(item_id, sealed_blob[, st])` into a single `.bfx` byte string."""
    idx_items = []
    blobs = []
    for entry in items:
        if isinstance(entry, dict):
            item_id = entry.get("id")
            blob = entry.get("blob")
            st = entry.get("st", 0)
        elif len(entry) == 2:
            item_id, blob = entry
            st = 0
        else:
            item_id, blob, st = entry[0], entry[1], entry[2]
        item_id = str(item_id or "")
        if not ID_RE.match(item_id):
            raise BfxError(f"bad item id: {item_id!r}")
        raw = bytes(blob)
        if not raw or len(raw) > MAX_ITEM_BYTES:
            raise BfxError("invalid sealed blob size")
        st = int(st) if isinstance(st, int) and not isinstance(st, bool) else 0
        if st not in ALLOWED_STATES:
            st = 0
        sha = hashlib.sha256(raw).hexdigest()
        idx_items.append({"id": item_id, "size": len(raw), "sha": sha, "st": st})
        blobs.append(raw)
    if not idx_items:
        raise BfxError("empty bundle")
    idx_doc = {"v": 1, "items": idx_items}
    idx_bytes = json.dumps(idx_doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(idx_bytes) > MAX_INDEX_BYTES:
        raise BfxError("index too large")
    return MAGIC + len(idx_bytes).to_bytes(4, "big") + idx_bytes + b"".join(blobs)


def pack(items, max_chunk_bytes=MAX_CHUNK_BYTES):
    """Pack items into one or more `.bfx` chunks, each at most `max_chunk_bytes` bytes."""
    max_b = int(max_chunk_bytes or MAX_CHUNK_BYTES)
    if max_b < 256:
        raise BfxError("max_chunk_bytes too small")
    normalized = []
    for entry in items or ():
        if isinstance(entry, dict):
            item_id, blob, st = entry.get("id"), entry.get("blob"), entry.get("st", 0)
        elif len(entry) == 2:
            item_id, blob, st = entry[0], entry[1], 0
        else:
            item_id, blob, st = entry[0], entry[1], entry[2]
        raw = bytes(blob)
        normalized.append((str(item_id or ""), raw, int(st) if isinstance(st, int) and not isinstance(st, bool) else 0))
    if not normalized:
        return []
    chunks = []
    cur = []
    cur_size = 64
    for item in normalized:
        entry_cost = len(item[1]) + 128
        if cur and cur_size + entry_cost > max_b:
            chunks.append(pack_one(cur))
            cur = []
            cur_size = 64
        cur.append(item)
        cur_size += entry_cost
    if cur:
        chunks.append(pack_one(cur))
    return chunks


def parse(data):
    """Parse and verify the outer structure and SHA-256 hashes of a `.bfx` bundle.

    Returns `(index_dict, [(entry_dict, sealed_bytes), ...])`.
    Raises `BfxError` if the magic, index, sizes, keys, or hashes are invalid or truncated.
    """
    if isinstance(data, str) and os.path.isfile(data):
        with open(data, "rb") as f:
            raw = f.read()
    else:
        raw = bytes(data)
    if len(raw) < 10 or raw[:4] != MAGIC:
        raise BfxError("not a .bfx bundle")
    idx_len = int.from_bytes(raw[4:8], "big")
    if idx_len < 2 or idx_len > MAX_INDEX_BYTES or len(raw) < 8 + idx_len:
        raise BfxError("truncated or damaged bundle header")
    try:
        idx = json.loads(raw[8:8 + idx_len].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise BfxError("damaged bundle index") from e
    if not isinstance(idx, dict) or idx.get("v") != 1:
        raise BfxError("unsupported bundle version")
    if set(idx.keys()) != ALLOWED_INDEX_KEYS:
        raise BfxError("unexpected fields in bundle index")
    items = idx.get("items")
    if not isinstance(items, list) or not items:
        raise BfxError("empty bundle index")

    pos = 8 + idx_len
    parsed = []
    for entry in items:
        if not isinstance(entry, dict) or set(entry.keys()) != ALLOWED_ITEM_KEYS:
            raise BfxError("unexpected or missing fields in bundle item entry")
        item_id = entry.get("id")
        size = entry.get("size")
        sha = entry.get("sha")
        st = entry.get("st")
        if not isinstance(item_id, str) or not ID_RE.match(item_id):
            raise BfxError("bad item id in bundle index")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size > MAX_ITEM_BYTES:
            raise BfxError("bad item size in bundle index")
        if not isinstance(sha, str) or not SHA_RE.match(sha):
            raise BfxError("bad item sha in bundle index")
        if not isinstance(st, int) or isinstance(st, bool) or st not in ALLOWED_STATES:
            raise BfxError("bad item state in bundle index")
        if pos + size > len(raw):
            raise BfxError("truncated bundle payload")
        blob = raw[pos:pos + size]
        pos += size
        actual_sha = hashlib.sha256(blob).hexdigest()
        if not hmac.compare_digest(actual_sha, sha):
            raise BfxError("tampered bundle payload (sha mismatch)")
        parsed.append((dict(entry), blob))

    if pos != len(raw):
        raise BfxError("trailing bytes in bundle")
    return idx, parsed


def import_bundle(source, store, handover):
    """Verify and import a `.bfx` bundle (bytes or file path) into `store` using `handover`.

    All items in the bundle are parsed and verified (index + SHA-256 + sealed envelope opening)
    before any item is written to `store`, so a truncated or tampered bundle is refused atomically.

    Returns `{"total": int, "imported": int, "skipped": int, "refused": [(id, reason)], "records": [rec]}`.
    """
    _idx, parsed = parse(source)
    opened = []
    for entry, sealed_blob in parsed:
        try:
            meta, payload = handover.open(sealed_blob)
        except ValueError as e:
            raise BfxError(f"cannot unseal item {entry['id']}: {e}") from e
        if not isinstance(meta, dict) or str(meta.get("id") or "") != entry["id"]:
            raise BfxError("sealed item id does not match bundle index")
        clean_m = dict(meta)
        clean_m.pop("preview", None)
        opened.append((entry, clean_m, payload))

    imported = 0
    skipped = 0
    refused = []
    records = []
    state_ids = store.jobs.state_ids()
    for entry, meta, payload in opened:
        item_id = entry["id"]
        ssha = entry["sha"]
        existing = store.records.get(item_id)
        if (existing and not existing.get("preview") and existing.get("photo") and os.path.isfile(existing["photo"])) or (
            item_id in state_ids
        ):
            store.ledger.mark(item_id, "office", sha=existing.get("sha") if existing else "", ssha=ssha)
            skipped += 1
            continue
        try:
            rec, new = store.save(meta, payload, ssha=ssha)
        except BlockedDeviceError as b:
            refused.append((item_id, b.reason))
            continue
        except (ValueError, TypeError) as e:
            refused.append((item_id, str(e)))
            continue
        if new:
            imported += 1
            if isinstance(rec, dict) and rec.get("photo"):
                records.append(rec)
        else:
            is_state = "cs" in meta or (isinstance(payload, (bytes, bytearray)) and bytes(payload)[:4] == b"BCS1")
            if is_state and item_id not in state_ids:
                imported += 1
                state_ids.add(item_id)
            else:
                skipped += 1
    return {
        "total": len(opened),
        "imported": imported,
        "skipped": skipped,
        "refused": refused,
        "records": records,
    }
