# Diwan — a GeoPackage that remembers

Version 0.2 (draft) · Biruni GIS

A Diwan file is an ordinary OGC GeoPackage. Every GIS program reads and writes its layers as usual. The Diwan
extension adds, in the same file, what a GeoPackage does not keep:

* a **permanent identity** for every feature (a UUID that survives exports, re-imports and renumbering);
* the **history** of every feature: each version with its time, who made it and how;
* what is needed to **convert** to any other format and **bring the edited copy back** without losing either.

Programs that do not know the extension ignore its tables and keep working; edits they make are found and
recorded the next time a Diwan-aware program opens the file (see *Reconciling*).

## Registration

A Diwan file is recognised by `diwan_meta.format = 'diwan'`. The extension is deliberately **not** listed in
`gpkg_extensions`: a program meeting an extension it does not know there warns that editing the layer is unsafe,
and every program must be able to edit a Diwan file freely (its edits are reconciled later).

One Diwan object is registered in `gpkg_contents`, as an `attributes` table: the read-only view
`diwan_history` (fid, layer, uid, change, at, who, how). Every program can show the history as a table, and GDAL -
which lists unregistered tables as layers while no attributes table is registered - stops listing the other Diwan
tables. They are not registered in `gpkg_contents`.

## Tables

```sql
CREATE TABLE diwan_meta (key TEXT PRIMARY KEY, value TEXT);
-- format = 'diwan', version = '0.1', file_id = UUID of this file, created = ISO time

CREATE TABLE diwan_tables (
  table_name TEXT PRIMARY KEY,   -- a table of gpkg_contents
  enabled_at TEXT NOT NULL);

CREATE TABLE diwan_ids (          -- the identity of each row
  table_name TEXT NOT NULL,
  fid INTEGER NOT NULL,          -- the row's primary key now (it may change: the uid does not)
  uid TEXT NOT NULL,             -- UUID, lower case, 36 characters
  PRIMARY KEY (table_name, fid),
  UNIQUE (table_name, uid));

CREATE TABLE diwan_log (          -- one row per version of a feature
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  uid TEXT NOT NULL,
  op TEXT NOT NULL CHECK (op IN ('add', 'change', 'delete')),
  at TEXT NOT NULL,              -- ISO 8601 UTC with milliseconds, e.g. 2026-11-03T10:42:07.123Z
  who TEXT,                      -- a person's name, or empty
  how TEXT,                      -- 'initial', 'biruni', 'external', 'import:<format>', 'restore', …
  geom BLOB,                     -- the geometry after the change: a GeoPackage geometry blob (NULL for delete)
  attrs TEXT,                    -- the attributes after the change: JSON object {column: value} (NULL for delete)
  state TEXT,                    -- SHA-256 of the state (see below); NULL for delete
  prev INTEGER);                 -- seq of the previous version of this uid, NULL for the first
CREATE INDEX diwan_log_uid ON diwan_log (table_name, uid, seq);

CREATE VIEW diwan_history AS      -- registered in gpkg_contents (data_type 'attributes')
  SELECT seq AS fid, table_name AS layer, uid, op AS change, at, who, how FROM diwan_log;
```

The last row of a uid is its current version. The table's own rows are the truth: the log describes how they
came to be.

### State hash

`state` = SHA-256 (hex) of: the geometry as ISO WKB (the GeoPackage blob without its header; empty for no
geometry), the byte `0x00`, then the attributes as JSON with sorted keys, no spaces, UTF-8, numbers as written by
the column type. Two rows with the same state hash have the same content.

Attribute values in JSON: TEXT as strings, INTEGER/REAL as numbers, NULL as null, BLOB as
`{"base64": "…"}`. The primary key column and the geometry column are not attributes.

## Reconciling (finding edits made elsewhere)

A Diwan-aware program compares each Diwan table with the current versions in `diwan_log`:

1. a row whose fid has a uid and whose state hash differs from that uid's last version → `change`;
2. a row whose fid has no uid → it is matched to a uid that lost its row and had exactly the same state (the
   row was renumbered, e.g. a program rewrote the table) — the uid moves to the new fid, nothing is logged;
   otherwise it gets a new uid → `add`;
3. a uid whose fid has no row any more and that was not matched in 2 → `delete`.

Versions found this way are logged with `how = 'external'` and the time of reconciling.

## Converting and bringing back

*Export* writes the table to any format with an extra text field `diwan_uid` holding each feature's uid, and a
sidecar `<file>.diwan.json`:

```json
{"diwan": "0.1", "file_id": "…", "table": "parcels", "exported_at": "…", "uids": ["…", "…"]}
```

*Bringing back* an edited copy: each feature of the copy is matched by its `diwan_uid`; a feature without one
(a format that lost the field, or a new feature) is matched by identical geometry and attributes, else it is
new. Features listed in the sidecar's `uids` and missing from the copy were deleted in the copy; features that
were not exported are never touched. The differences are written to the table and logged with
`how = 'import:<format>'`.

## Branches and synchronising (0.2)

A *branch* is a copy of a Diwan file (made with SQLite's backup, so it is consistent) that goes away - a field
laptop, a colleague - and is later synchronised with the file it came from, both ways at once.

```sql
-- in the main file
CREATE TABLE diwan_branches (
  branch_id TEXT PRIMARY KEY, name TEXT, created TEXT NOT NULL,
  main_synced INTEGER NOT NULL,     -- the main file's last seq at the last sync
  branch_synced INTEGER NOT NULL,   -- the branch's last seq at the last sync
  last_sync TEXT);
CREATE TABLE diwan_conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, uid TEXT NOT NULL, at TEXT NOT NULL,
  branch TEXT, what TEXT NOT NULL,  -- 'field', 'geometry' or 'deleted'
  field TEXT, kept TEXT, other TEXT, resolved INTEGER NOT NULL DEFAULT 0);
```

The branch's `diwan_meta` adds `role = 'branch'`, `branch_id`, `branch_name`, `main_file_id`, `main_path`,
`main_synced`, `branch_synced`, and the branch gets its own `file_id`.

**Synchronising.** Both files are reconciled first. A uid is *changed* on a side when that side logged a version of
it after its sync point. Then, for each changed uid:

* changed on one side only → that side's current state is written on the other side;
* changed on both, to the same state → nothing;
* deleted on one side, changed on the other → the change is kept on both sides, and a `deleted` conflict is listed;
* changed on both → three-way merge, the base being the branch's version at its sync point: attributes field by
  field, the geometry vertex by vertex (edits on different vertices are all kept); where both sides changed the
  same thing differently the main file's value is kept on both sides and a conflict is listed.

Writes are logged with `how = 'sync:<branch name>'` in the main file and `'sync:main'` in the branch; then both
sync points move to the files' last seq. After a sync both files hold the same features. A conflict can later be
turned around in the main file (`how = 'resolve'`); it reaches the branch at the next sync.

## Versioning of this specification

`diwan_meta.version` names the version a file follows. Readers must ignore tables, columns and `how` values they
do not know. Later versions add (branches and merging, seals, accuracy and provenance, attachments); they do not
change the meaning of the tables above.
