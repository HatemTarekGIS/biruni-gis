"""Diwan: a GeoPackage that remembers (SPEC.md beside this file).

A library on its own - Python's sqlite3 and GDAL's osgeo only, no Qt, no Biruni - so any program can read and
write Diwan files. The feature tables themselves are written only through GDAL: a GeoPackage keeps its spatial
index up to date with triggers that call GDAL's own SQL functions, which a plain SQLite connection does not have.
The Diwan tables have no triggers and are written with sqlite3.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import sqlite3
import struct
import uuid

EXT = "biruni_diwan"
VERSION = "0.1"
SIDE = ".diwan.json"          # the sidecar written beside an export
UID_FIELD = "diwan_uid"       # the field an export carries (fits a shapefile's 10 characters)

SCHEMA = """
CREATE TABLE IF NOT EXISTS diwan_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS diwan_tables (table_name TEXT PRIMARY KEY, enabled_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS diwan_ids (
  table_name TEXT NOT NULL, fid INTEGER NOT NULL, uid TEXT NOT NULL,
  PRIMARY KEY (table_name, fid), UNIQUE (table_name, uid));
CREATE TABLE IF NOT EXISTS diwan_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, uid TEXT NOT NULL,
  op TEXT NOT NULL CHECK (op IN ('add', 'change', 'delete')), at TEXT NOT NULL, who TEXT, how TEXT,
  geom BLOB, attrs TEXT, state TEXT, prev INTEGER);
CREATE INDEX IF NOT EXISTS diwan_log_uid ON diwan_log (table_name, uid, seq);
CREATE VIEW IF NOT EXISTS diwan_history AS
  SELECT seq AS fid, table_name AS layer, uid, op AS change, at, who, how FROM diwan_log;
"""
HISTORY = "diwan_history"     # the one table other programs list: the history, readable everywhere


class DiwanError(Exception):
    """Something the user can act on (the message says what)."""


def now():
    """UTC with milliseconds, the form GeoPackage asks for (2026-11-03T10:42:07.123Z)."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---- the state of a row ---------------------------------------------------------------------------------
def wkb_of(blob):
    """The ISO WKB inside a GeoPackage geometry blob (b"" for none)."""
    if not blob:
        return b""
    blob = bytes(blob)
    if blob[:2] != b"GP":
        return blob                                   # already WKB
    flags = blob[3]
    env = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((flags >> 1) & 7, 0)
    return blob[8 + env:]


def norm(v):
    """A value as it goes into the JSON of a state: integral floats as integers, blobs as base64."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"base64": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, float) and v.is_integer() and abs(v) < 2 ** 53:
        return int(v)
    return v


def attrs_json(attrs):
    return json.dumps({k: norm(v) for k, v in attrs.items()}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def state_hash(geom, attrs):
    return hashlib.sha256(wkb_of(geom) + b"\x00" + attrs_json(attrs).encode("utf-8")).hexdigest()


def _same_geometry(wkb_a, wkb_b):
    """True when two WKB geometries are the same shape (ring direction, start vertex, single / multi of one
    part do not matter) - a shapefile writes rings its own way."""
    if wkb_a == wkb_b:
        return True
    if not wkb_a or not wkb_b:
        return False
    from osgeo import ogr

    a, b = ogr.CreateGeometryFromWkb(bytes(wkb_a)), ogr.CreateGeometryFromWkb(bytes(wkb_b))
    if a is None or b is None:
        return False
    multi = (ogr.wkbMultiPolygon, ogr.wkbMultiLineString, ogr.wkbMultiPoint)

    def single(g):   # a multi geometry of one part compares as that part
        return g.GetGeometryRef(0).Clone() if ogr.GT_Flatten(g.GetGeometryType()) in multi and \
            g.GetGeometryCount() == 1 else g
    a, b = single(a), single(b)
    try:   # OGR's Equals compares vertex by vertex: normalise first (ring direction, start vertex)
        a.Normalize()
        b.Normalize()
        if a.ExportToIsoWkb() == b.ExportToIsoWkb():
            return True
        d = a.SymDifference(b)
        return d is None or d.IsEmpty() or (d.GetArea() if d.GetDimension() == 2 else d.Length()) <= 1e-12
    except Exception:  # noqa: BLE001 - GEOS could not compare: not the same
        return False


# ---- the file ---------------------------------------------------------------------------------------------
class Diwan:
    """A GeoPackage opened as a Diwan (``enable`` turns an ordinary GeoPackage into one)."""

    def __init__(self, path):
        if not os.path.exists(path):
            raise DiwanError(f"No such file: {path}")
        self.path = path
        self.con = sqlite3.connect(path, timeout=30)

    def close(self):
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # -- what the file holds --
    @staticmethod
    def is_diwan(path):
        try:
            con = sqlite3.connect(path, timeout=5)
            try:
                return con.execute("SELECT value FROM diwan_meta WHERE key='format'").fetchone() == ("diwan",)
            finally:
                con.close()
        except sqlite3.Error:
            return False

    def meta(self, key):
        r = self.con.execute("SELECT value FROM diwan_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def tables(self):
        try:
            return [r[0] for r in self.con.execute("SELECT table_name FROM diwan_tables ORDER BY table_name")]
        except sqlite3.Error:
            return []

    def layer_tables(self):
        """The feature and attribute tables of the GeoPackage (not the history view)."""
        return [r[0] for r in self.con.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type IN ('features', 'attributes') AND table_name != ? "
            "ORDER BY table_name", (HISTORY,))]

    def _columns(self, table):
        info = self.con.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not info:
            raise DiwanError(f"No table {table} in the file.")
        pk = next((r[1] for r in info if r[5] == 1), None)
        g = self.con.execute("SELECT column_name FROM gpkg_geometry_columns WHERE table_name=?", (table,)).fetchone()
        geom = g[0] if g else None
        attrs = [r[1] for r in info if r[1] not in (pk, geom)]
        return pk, geom, attrs

    def rows(self, table, fids=None):
        """(fid, geometry blob, {column: value}) of the table's rows (only ``fids`` when given)."""
        pk, geom, attrs = self._columns(table)
        cols = ", ".join(f'"{c}"' for c in [pk] + ([geom] if geom else []) + attrs)
        sql = f'SELECT {cols} FROM "{table}"'
        if fids is not None:
            fids = list(fids)
            out = []
            for i in range(0, len(fids), 500):
                part = fids[i:i + 500]
                out += self.con.execute(sql + f' WHERE "{pk}" IN ({",".join("?" * len(part))})', part).fetchall()
            found = out
        else:
            found = self.con.execute(sql)
        n = 2 if geom else 1
        for r in found:
            yield r[0], (r[1] if geom else None), dict(zip(attrs, r[n:]))

    # -- turning it on --
    def enable(self, tables=None, who=""):
        """Make the file a Diwan and give every row of ``tables`` (default: every layer) its identity and first
        version. -> number of features registered."""
        tables = tables or self.layer_tables()
        n = 0
        with self.con:
            self.con.executescript(SCHEMA)
            for k, v in (("format", "diwan"), ("version", VERSION), ("file_id", str(uuid.uuid4())), ("created", now())):
                self.con.execute("INSERT OR IGNORE INTO diwan_meta VALUES (?, ?)", (k, v))
            # the history view is the only Diwan table registered: other programs list it (read-only) and, once
            # an attributes table is registered, GDAL stops listing the unregistered ones as layers. The extension
            # is NOT put in gpkg_extensions: there, an extension a program does not know makes it warn that
            # editing the layer is unsafe - and every program may edit a Diwan file.
            self.con.execute("INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, description, "
                             "last_change) VALUES (?, 'attributes', 'Diwan history', ?, ?)",
                             (HISTORY, "Every version of every feature: when, who and how (Diwan)", now()))
            for t in tables:
                if self.con.execute("SELECT 1 FROM diwan_tables WHERE table_name=?", (t,)).fetchone():
                    continue
                self._columns(t)
                self.con.execute("INSERT INTO diwan_tables VALUES (?, ?)", (t, now()))
                at = now()
                for fid, g, a in self.rows(t):
                    uid = str(uuid.uuid4())
                    self.con.execute("INSERT INTO diwan_ids VALUES (?, ?, ?)", (t, fid, uid))
                    self._log(t, uid, "add", at, who, "initial", g, a, None)
                    n += 1
        return n

    # -- the log --
    def _log(self, table, uid, op, at, who, how, geom, attrs, prev):
        if op == "delete":
            vals = (table, uid, op, at, who, how, None, None, None, prev)
        else:
            vals = (table, uid, op, at, who, how, geom, attrs_json(attrs), state_hash(geom, attrs), prev)
        cur = self.con.execute("INSERT INTO diwan_log (table_name, uid, op, at, who, how, geom, attrs, state, prev) "
                               "VALUES (?,?,?,?,?,?,?,?,?,?)", vals)
        return cur.lastrowid

    def current(self, table):
        """{uid: (seq, op, state)} - the last version of every uid of the table (deleted ones too)."""
        return {r[0]: (r[1], r[2], r[3]) for r in self.con.execute(
            "SELECT uid, seq, op, state FROM diwan_log WHERE seq IN "
            "(SELECT MAX(seq) FROM diwan_log WHERE table_name=? GROUP BY uid)", (table,))}

    def ids(self, table):
        return {r[0]: r[1] for r in self.con.execute("SELECT fid, uid FROM diwan_ids WHERE table_name=?", (table,))}

    def uid_of(self, table, fid):
        r = self.con.execute("SELECT uid FROM diwan_ids WHERE table_name=? AND fid=?", (table, fid)).fetchone()
        return r[0] if r else None

    def fid_of(self, table, uid):
        r = self.con.execute("SELECT fid FROM diwan_ids WHERE table_name=? AND uid=?", (table, uid)).fetchone()
        return r[0] if r else None

    def history(self, table, uid):
        """The versions of a feature, oldest first: [{seq, op, at, who, how, geom, attrs, state}]."""
        out = []
        for r in self.con.execute("SELECT seq, op, at, who, how, geom, attrs, state FROM diwan_log "
                                  "WHERE table_name=? AND uid=? ORDER BY seq", (table, uid)):
            out.append({"seq": r[0], "op": r[1], "at": r[2], "who": r[3] or "", "how": r[4] or "", "geom": r[5],
                        "attrs": json.loads(r[6]) if r[6] else None, "state": r[7]})
        return out

    def version(self, seq):
        r = self.con.execute("SELECT table_name, uid, op, at, who, how, geom, attrs FROM diwan_log WHERE seq=?",
                             (seq,)).fetchone()
        if r is None:
            return None
        return {"table": r[0], "uid": r[1], "op": r[2], "at": r[3], "who": r[4] or "", "how": r[5] or "",
                "geom": r[6], "attrs": json.loads(r[7]) if r[7] else None}

    def state_at(self, table, when):
        """{uid: {"geom", "attrs", "at"}} of the features that existed at ``when`` (ISO time), as they were."""
        out = {}
        for r in self.con.execute(
                "SELECT uid, op, geom, attrs, at FROM diwan_log WHERE seq IN (SELECT MAX(seq) FROM diwan_log "
                "WHERE table_name=? AND at<=? GROUP BY uid)", (table, when)):
            if r[1] != "delete":
                out[r[0]] = {"geom": r[2], "attrs": json.loads(r[3]) if r[3] else {}, "at": r[4]}
        return out

    def changes(self, table=None, since=None, limit=200):
        """The last versions logged, newest first (for a "what happened" list)."""
        sql = "SELECT seq, table_name, uid, op, at, who, how FROM diwan_log WHERE how != 'initial'"
        args = []
        if table:
            sql += " AND table_name=?"
            args.append(table)
        if since:
            sql += " AND at>=?"
            args.append(since)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        return [dict(zip(("seq", "table", "uid", "op", "at", "who", "how"), r)) for r in self.con.execute(sql, args)]

    # -- reconciling: what changed since the log was last written --
    def reconcile(self, table=None, who="", how="external", fids=None):
        """Log the differences between the tables and their last versions (SPEC.md, Reconciling). With ``fids``
        only those rows are looked at (after a save that changed them). -> {"add", "change", "delete", "moved"}."""
        totals = {"add": 0, "change": 0, "delete": 0, "moved": 0}
        for t in ([table] if table else self.tables()):
            with self.con:
                for k, v in self._reconcile(t, who, how, fids).items():
                    totals[k] += v
        return totals

    def _reconcile(self, t, who, how, fids):
        at = now()
        ids = self.ids(t)                       # fid -> uid
        cur = self.current(t)                   # uid -> (seq, op, state)
        counts = {"add": 0, "change": 0, "delete": 0, "moved": 0}
        seen_fids, new_rows = set(), []
        for fid, g, a in self.rows(t, fids):
            seen_fids.add(fid)
            uid = ids.get(fid)
            if uid is None:
                new_rows.append((fid, g, a))
                continue
            seq, op, state = cur.get(uid, (None, "delete", None))
            if op == "delete" or state != state_hash(g, a):
                self._log(t, uid, "change" if op != "delete" else "add", at, who, how, g, a, seq)
                counts["change"] += 1
        if fids is None:
            gone = [fid for fid in ids if fid not in seen_fids]
        else:
            gone = [fid for fid in fids if fid not in seen_fids and fid in ids]
        lost = {}                               # state -> [uid] of rows that vanished (maybe renumbered)
        for fid in gone:
            uid = ids[fid]
            seq, op, state = cur.get(uid, (None, "delete", None))
            if op != "delete":
                lost.setdefault(state, []).append((fid, uid, seq))
        for fid, g, a in new_rows:
            st = state_hash(g, a)
            if lost.get(st):
                old_fid, uid, _seq = lost[st].pop(0)
                self.con.execute("DELETE FROM diwan_ids WHERE table_name=? AND fid=?", (t, old_fid))
                self.con.execute("DELETE FROM diwan_ids WHERE table_name=? AND fid=?", (t, fid))
                self.con.execute("INSERT INTO diwan_ids VALUES (?, ?, ?)", (t, fid, uid))
                counts["moved"] += 1
                continue
            uid = str(uuid.uuid4())
            self.con.execute("DELETE FROM diwan_ids WHERE table_name=? AND fid=?", (t, fid))
            self.con.execute("INSERT INTO diwan_ids VALUES (?, ?, ?)", (t, fid, uid))
            self._log(t, uid, "add", at, who, how, g, a, None)
            counts["add"] += 1
        for items in lost.values():
            for fid, uid, seq in items:
                self.con.execute("DELETE FROM diwan_ids WHERE table_name=? AND fid=?", (t, fid))
                self._log(t, uid, "delete", at, who, how, None, None, seq)
                counts["delete"] += 1
        return counts

    # -- writing rows (through GDAL) --
    def _ogr(self):
        from osgeo import gdal

        gdal.UseExceptions()
        ds = gdal.OpenEx(self.path, gdal.OF_VECTOR | gdal.OF_UPDATE)
        if ds is None:
            raise DiwanError(f"Cannot open {self.path} for writing.")
        return ds

    @staticmethod
    def _fill(feat, defn, geom_wkb, attrs):
        from osgeo import ogr

        if defn.GetGeomFieldCount():
            feat.SetGeometry(ogr.CreateGeometryFromWkb(bytes(geom_wkb)) if geom_wkb else None)
        for name, v in attrs.items():
            i = defn.GetFieldIndex(name)
            if i < 0:
                continue
            if v is None:
                feat.SetFieldNull(i)
            elif isinstance(v, dict) and "base64" in v:
                feat.SetFieldBinaryFromHexString(i, base64.b64decode(v["base64"]).hex())
            else:
                feat.SetField(i, v)

    def restore(self, seq, who=""):
        """Put a feature back as it was in version ``seq`` (logged how='restore'). -> its fid."""
        v = self.version(seq)
        if v is None or v["op"] == "delete":
            raise DiwanError("That version is a deletion: there is nothing to put back.")
        t, uid = v["table"], v["uid"]
        ds = self._ogr()
        lyr = ds.GetLayerByName(t)
        defn = lyr.GetLayerDefn()
        fid = self.fid_of(t, uid)
        from osgeo import ogr

        feat = lyr.GetFeature(fid) if fid is not None else None
        if feat is None:
            feat = ogr.Feature(defn)
            self._fill(feat, defn, wkb_of(v["geom"]), v["attrs"])
            lyr.CreateFeature(feat)
            fid = feat.GetFID()
        else:
            self._fill(feat, defn, wkb_of(v["geom"]), v["attrs"])
            lyr.SetFeature(feat)
        ds = None   # noqa: F841 - written and closed
        with self.con:
            self.con.execute("DELETE FROM diwan_ids WHERE table_name=? AND (fid=? OR uid=?)", (t, fid, uid))
            self.con.execute("INSERT INTO diwan_ids VALUES (?, ?, ?)", (t, fid, uid))
        self.reconcile(t, who, "restore", fids=[fid])
        return fid

    # -- converting --
    def export(self, table, out_path, driver=None):
        """Write ``table`` to any vector format GDAL writes (by ``driver`` or the file extension), each feature
        with its uid in a ``diwan_uid`` field, plus the sidecar out_path + '.diwan.json'. -> features written."""
        from osgeo import gdal, ogr

        gdal.UseExceptions()
        src = gdal.OpenEx(self.path, gdal.OF_VECTOR)
        slyr = src.GetLayerByName(table)
        if slyr is None:
            raise DiwanError(f"No layer {table}.")
        if driver is None:
            ext = os.path.splitext(out_path)[1].lower()
            driver = {".shp": "ESRI Shapefile", ".geojson": "GeoJSON", ".json": "GeoJSON", ".gpkg": "GPKG",
                      ".kml": "KML", ".csv": "CSV", ".dxf": "DXF", ".gml": "GML", ".fgb": "FlatGeobuf",
                      ".gdb": "OpenFileGDB", ".tab": "MapInfo File", ".sqlite": "SQLite"}.get(ext)
            if driver is None:
                raise DiwanError(f"Unknown format for {out_path}.")
        drv = ogr.GetDriverByName(driver)
        if os.path.exists(out_path):
            drv.DeleteDataSource(out_path)
        dst = drv.CreateDataSource(out_path)
        sdefn = slyr.GetLayerDefn()
        dlyr = dst.CreateLayer(table, slyr.GetSpatialRef(), slyr.GetGeomType())
        names = []
        for i in range(sdefn.GetFieldCount()):
            dlyr.CreateField(sdefn.GetFieldDefn(i))
            names.append(sdefn.GetFieldDefn(i).GetName())
        dlyr.CreateField(ogr.FieldDefn(UID_FIELD, ogr.OFTString))
        ddefn = dlyr.GetLayerDefn()
        uid_idx = ddefn.GetFieldIndex(UID_FIELD)
        ids = self.ids(table)
        uids = []
        for f in slyr:
            out = ogr.Feature(ddefn)
            g = f.GetGeometryRef()
            if g is not None:
                out.SetGeometry(g.Clone())
            for i, n in enumerate(names):
                j = ddefn.GetFieldIndex(n)
                if j < 0 and len(n) > 10:            # a shapefile shortens long names
                    j = i
                if j >= 0 and not f.IsFieldNull(i):
                    out.SetField(j, f.GetField(i))
            uid = ids.get(f.GetFID())
            if uid and uid_idx >= 0:
                out.SetField(uid_idx, uid)
                uids.append(uid)
            dlyr.CreateFeature(out)
        dst = src = None   # noqa: F841 - written and closed
        with open(out_path + SIDE, "w", encoding="utf-8") as fh:
            json.dump({"diwan": VERSION, "file_id": self.meta("file_id"), "table": table, "exported_at": now(),
                       "driver": driver, "uids": uids}, fh, ensure_ascii=False)
        return len(uids)

    def bring_back(self, table, copy_path, who="", layer_name=None):
        """Apply an edited copy made with ``export`` (SPEC.md, Converting and bringing back).
        -> {"add", "change", "delete", "moved", "same"}."""
        from osgeo import gdal, ogr

        gdal.UseExceptions()
        side = {}
        if os.path.exists(copy_path + SIDE):
            with open(copy_path + SIDE, encoding="utf-8") as fh:
                side = json.load(fh)
            if side.get("file_id") not in (None, self.meta("file_id")):
                raise DiwanError("This copy was exported from another file.")
        exported = set(side.get("uids") or [])
        cp = gdal.OpenEx(copy_path, gdal.OF_VECTOR)
        clyr = cp.GetLayerByName(layer_name) if layer_name else cp.GetLayer(0)
        cdefn = clyr.GetLayerDefn()
        _pk, _g, attrs = self._columns(table)
        # copy field -> table column: same name, else a shapefile's shortened name
        cmap = {}
        for i in range(cdefn.GetFieldCount()):
            n = cdefn.GetFieldDefn(i).GetName()
            if n == UID_FIELD:
                continue
            col = next((c for c in attrs if c == n), None) or \
                next((c for c in attrs if c[:10].lower() == n.lower()), None)
            if col:
                cmap[i] = col
        uid_idx = cdefn.GetFieldIndex(UID_FIELD)
        ids = self.ids(table)
        by_uid = {u: f for f, u in ids.items()}
        rows = {fid: (g, a) for fid, g, a in self.rows(table)}
        by_state = {}
        for fid, (g, a) in rows.items():
            by_state.setdefault(state_hash(g, a), []).append(fid)

        def same_value(a, b):
            if a is None or a == "":
                return b is None or b == ""
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))
            return str(a) == str(b)

        plan_set, plan_add, matched = [], [], set()
        counts = {"add": 0, "change": 0, "delete": 0, "moved": 0, "same": 0}
        for f in clyr:
            g = f.GetGeometryRef()
            wkb = bytes(g.ExportToIsoWkb()) if g is not None else b""
            vals = {col: (None if f.IsFieldNull(i) else f.GetField(i)) for i, col in cmap.items()}
            uid = f.GetField(uid_idx) if uid_idx >= 0 and not f.IsFieldNull(uid_idx) else None
            fid = by_uid.get(uid) if uid else None
            if fid is None:   # no uid: the same content is the same feature
                cand = by_state.get(state_hash(wkb, {**{c: None for c in attrs}, **vals}))
                fid = next((c for c in cand or [] if c not in matched), None)
            if fid is None or fid not in rows:
                plan_add.append((wkb, vals))
                continue
            matched.add(fid)
            old_g, old_a = rows[fid]
            geom_same = _same_geometry(wkb_of(old_g), wkb)
            attr_same = all(same_value(vals.get(c), old_a.get(c)) for c in cmap.values())
            if geom_same and attr_same:
                counts["same"] += 1
                continue
            plan_set.append((fid, None if geom_same else wkb, {c: vals[c] for c in cmap.values()
                                                               if not same_value(vals.get(c), old_a.get(c))}))
        plan_del = [fid for fid, uid in ids.items() if uid in exported and fid not in matched and fid in rows]
        if not (plan_set or plan_add or plan_del):
            return counts
        ds = self._ogr()
        lyr = ds.GetLayerByName(table)
        defn = lyr.GetLayerDefn()
        touched = []
        ds.StartTransaction()
        try:
            for fid, wkb, changed in plan_set:
                feat = lyr.GetFeature(fid)
                if wkb is not None:
                    feat.SetGeometry(ogr.CreateGeometryFromWkb(wkb) if wkb else None)
                for col, v in changed.items():
                    i = defn.GetFieldIndex(col)
                    if v is None:
                        feat.SetFieldNull(i)
                    else:
                        feat.SetField(i, v)
                lyr.SetFeature(feat)
                touched.append(fid)
            for wkb, vals in plan_add:
                feat = ogr.Feature(defn)
                self._fill(feat, defn, wkb, vals)
                lyr.CreateFeature(feat)
                touched.append(feat.GetFID())
            for fid in plan_del:
                lyr.DeleteFeature(fid)
                touched.append(fid)
            ds.CommitTransaction()
        except Exception:
            ds.RollbackTransaction()
            raise
        finally:
            ds = None   # noqa: F841
        fmt = side.get("driver") or os.path.splitext(copy_path)[1].lstrip(".")
        found = self.reconcile(table, who, f"import:{fmt}", fids=touched)
        for k, v in found.items():
            counts[k] += v
        return counts
