"""Edits that survive a crash or a power cut.

Edits that are not saved yet live only in memory (each layer's edit buffer). The
journal keeps a copy of that buffer on disk while you work, so if Biruni or the
computer stops suddenly the next start can put the edits back:

* every vector layer in edit mode is watched; a few seconds after the last change
  (and at least every 15 s while editing goes on) the *current* state of its
  buffer is written - new features, changed geometries and values, deleted
  features, added and removed fields - only for the features that changed since
  the last write, so even a buffer of tens of thousands of edits is written in
  small steps;
* one SQLite file per project, ``<project folder>/.biruni/recovery/<Name>.journal.sqlite``
  (a temporary map: ``%APPDATA%/Biruni GIS/autosave/temporary.journal.sqlite``),
  in WAL mode with full sync: a write either reaches the disk whole or not at all;
* saving or discarding a layer's edits removes its part; saving the project with no
  layer in edit mode, closing it, or quitting normally removes the file - so a file
  that is still there was left by a crash;
* nothing is read or written while no layer is being edited.

Temporary (memory) layers are not journaled: the automatic project copy keeps them.
Restoring puts the edits back into the layers' edit buffers (one undo step per layer)
and leaves them unsaved, so they can be checked before saving.
"""
import base64
import datetime as dt
import json
import math
import os
import sqlite3
import time
import uuid

from qgis.core import (QgsFeature, QgsField, QgsGeometry, QgsProject, QgsProviderRegistry, QgsSettings,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import QDate, QDateTime, QMetaType, QObject, Qt, QTime, QTimer

ENABLED_KEY = "biruni/autosave/edit-journal"
QUIET_MS = 3_000        # write this long after the last change ...
MAX_WAIT_S = 15.0       # ... but at least this often while editing goes on
SUFFIX = ".journal.sqlite"
SCHEMA = 1
CHUNK = 1500            # features per timed write (about 0.1 s): big buffers are written in steps
SESSION = uuid.uuid4().hex  # journals written by this run of Biruni are never offered back to it


def enabled():
    return QgsSettings().value(ENABLED_KEY, True, type=bool)


def set_enabled(on):
    QgsSettings().setValue(ENABLED_KEY, bool(on))


def temporary_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Biruni GIS", "autosave")


def _stem(project_file, temp_dir=None):
    if project_file:
        folder = os.path.dirname(project_file)
        base = os.path.splitext(os.path.basename(project_file))[0]
        return os.path.join(folder, ".biruni", "recovery", base)
    return os.path.join(temp_dir or temporary_dir(), "temporary")


def journal_path(project_file, temp_dir=None, session=None):
    """Where this session keeps the journal of a project (a temporary map when ``project_file`` is empty).

    Each run of Biruni writes its own file (``<Name>.<session>.journal.sqlite``), so a journal left by a crash is
    never overwritten before it was offered."""
    return f"{_stem(project_file, temp_dir)}.{(session or SESSION)[:12]}{SUFFIX}"


def journals_left(project_file, temp_dir=None):
    """Journal files of this project written by other (earlier) sessions, newest first."""
    import glob

    stem = _stem(project_file, temp_dir)
    ours = journal_path(project_file, temp_dir)
    found = [p for p in glob.glob(glob.escape(stem) + ".*" + SUFFIX) if os.path.normcase(p) != os.path.normcase(ours)]
    return sorted(found, key=_mtime, reverse=True)


def _mtime(path):
    return max([os.path.getmtime(p) for p in (path, path + "-wal") if os.path.exists(p)] or [0])


def _hide(path):
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(path, 0x02)
    except Exception:
        pass


def remove_file(path):
    for p in (path, path + "-wal", path + "-shm", path + "-journal"):
        try:
            os.remove(p)
        except OSError:
            pass


# ---- values ------------------------------------------------------------------------------------------
def encode(v):
    """A field value as JSON (dates, binary, lists and maps tagged so they come back as they were)."""
    if v is None:
        return None
    if hasattr(v, "isNull") and not isinstance(v, (QDate, QDateTime, QTime)) and callable(v.isNull) and v.isNull():
        return None  # QGIS NULL
    if hasattr(v, "value") and type(v).__name__ == "QVariant":
        v = v.value()
        if v is None:
            return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else {"$": "float", "v": repr(v)}
    if isinstance(v, str):
        return v
    if isinstance(v, QDateTime):
        return None if not v.isValid() else {"$": "datetime", "v": v.toString(Qt.DateFormat.ISODateWithMs)}
    if isinstance(v, QDate):
        return None if not v.isValid() else {"$": "date", "v": v.toString(Qt.DateFormat.ISODate)}
    if isinstance(v, QTime):
        return None if not v.isValid() else {"$": "time", "v": v.toString(Qt.DateFormat.ISODateWithMs)}
    if isinstance(v, dt.datetime):
        return {"$": "datetime", "v": v.isoformat()}
    if isinstance(v, dt.date):
        return {"$": "date", "v": v.isoformat()}
    if isinstance(v, dt.time):
        return {"$": "time", "v": v.isoformat()}
    if isinstance(v, (bytes, bytearray)) or type(v).__name__ == "QByteArray":
        return {"$": "bytes", "v": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, (list, tuple)):
        return {"$": "list", "v": [encode(x) for x in v]}
    if isinstance(v, dict):
        return {"$": "map", "v": {str(k): encode(x) for k, x in v.items()}}
    return {"$": "str", "v": str(v)}


def decode(v):
    if not isinstance(v, dict):
        return v
    kind, val = v.get("$"), v.get("v")
    if kind == "datetime":
        return QDateTime.fromString(val, Qt.DateFormat.ISODateWithMs)
    if kind == "date":
        return QDate.fromString(val, Qt.DateFormat.ISODate)
    if kind == "time":
        return QTime.fromString(val, Qt.DateFormat.ISODateWithMs)
    if kind == "bytes":
        return base64.b64decode(val)
    if kind == "list":
        return [decode(x) for x in val]
    if kind == "map":
        return {k: decode(x) for k, x in val.items()}
    if kind == "float":
        return float(val)
    return val


def _field_dict(f):
    return {"name": f.name(), "type": int(f.type().value) if hasattr(f.type(), "value") else int(f.type()),
            "type_name": f.typeName(), "length": f.length(), "precision": f.precision(),
            "sub_type": int(f.subType().value) if hasattr(f.subType(), "value") else int(f.subType())}


def _field_from(d):
    f = QgsField(d["name"], QMetaType.Type(d["type"]), d.get("type_name") or "", d.get("length") or 0,
                 d.get("precision") or 0)
    try:
        f.setSubType(QMetaType.Type(d.get("sub_type") or 0))
    except Exception:
        pass
    return f


def _geom_bytes(g):
    if g is None or g.isNull():
        return None
    return bytes(g.asWkb())


# ---- layer identity --------------------------------------------------------------------------------------
def _norm(source):
    return (source or "").replace("\\", "/").strip().lower()


def journaled(layer):
    """Layers whose edits the journal keeps (file and database layers; not temporary ones)."""
    return isinstance(layer, QgsVectorLayer) and layer.isValid() and layer.providerType() not in ("memory",)


def source_state(layer):
    """What the edits were based on: the data source's file (size/time) and feature count."""
    path = ""
    try:
        path = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source()).get("path") or ""
    except Exception:
        pass
    size = mtime = None
    if path and os.path.isfile(path):
        st = os.stat(path)
        size, mtime = st.st_size, st.st_mtime
    try:
        count = int(layer.dataProvider().featureCount())
    except Exception:
        count = -1
    return {"path": path, "size": size, "mtime": mtime, "count": count}


# ---- the file ---------------------------------------------------------------------------------------------
class JournalFile:
    """The SQLite journal of one project."""

    def __init__(self, path):
        self.path = path
        self.con = None

    def open(self):
        if self.con is None:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            hidden_dir = os.path.basename(os.path.dirname(os.path.dirname(self.path))) == ".biruni"
            self.con = sqlite3.connect(self.path, timeout=5)
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("PRAGMA synchronous=FULL")
            self.con.executescript(
                "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);"
                "CREATE TABLE IF NOT EXISTS layers(layer_id TEXT PRIMARY KEY, name TEXT, source TEXT, provider TEXT,"
                " crs TEXT, base TEXT, added_fields TEXT, deleted_fields TEXT, updated REAL);"
                "CREATE TABLE IF NOT EXISTS feats(layer_id TEXT, fid INTEGER, kind TEXT, geom BLOB, attrs TEXT,"
                " PRIMARY KEY(layer_id, fid));")
            with self.con:
                self.con.executemany("INSERT OR REPLACE INTO meta VALUES(?,?)",
                                     [("schema", str(SCHEMA)), ("session", SESSION),
                                      ("project", QgsProject.instance().fileName())])
            if hidden_dir:
                _hide(os.path.dirname(os.path.dirname(self.path)))
            _hide(self.path)
        return self.con

    def close(self):
        if self.con is not None:
            try:
                self.con.close()
            except Exception:
                pass
            self.con = None

    def delete(self):
        self.close()
        remove_file(self.path)

    def is_empty(self):
        if self.con is None:
            return not os.path.exists(self.path)
        return self.con.execute("SELECT COUNT(*) FROM layers").fetchone()[0] == 0

    def drop_layer(self, layer_id):
        if self.con is None:
            return
        with self.con:
            self.con.execute("DELETE FROM feats WHERE layer_id=?", (layer_id,))
            self.con.execute("DELETE FROM layers WHERE layer_id=?", (layer_id,))


def read_journal(path):
    """The layers kept in a journal file: a list of dicts, or [] when the file is missing or unreadable
    (a write cut short by a power failure is rolled back by SQLite; a damaged file is ignored)."""
    if not path or not os.path.exists(path):
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute("SELECT layer_id, name, source, provider, crs, base, added_fields, deleted_fields,"
                           " updated FROM layers").fetchall()
        out = []
        for lid, name, source, provider, crs, base, af, df, updated in rows:
            feats = con.execute("SELECT fid, kind, geom, attrs FROM feats WHERE layer_id=?", (lid,)).fetchall()
            entry = {"layer_id": lid, "name": name, "source": source, "provider": provider, "crs": crs,
                     "base": json.loads(base or "{}"), "added_fields": json.loads(af or "[]"),
                     "deleted_fields": json.loads(df or "[]"), "updated": updated, "added": [], "changed": [],
                     "deleted": []}
            for fid, kind, geom, attrs in feats:
                rec = {"fid": fid, "geom": geom, "attrs": json.loads(attrs) if attrs else {}}
                {"add": entry["added"], "chg": entry["changed"], "del": entry["deleted"]}.get(kind, []).append(rec)
            if entry["added"] or entry["changed"] or entry["deleted"] or entry["added_fields"] or \
                    entry["deleted_fields"]:
                out.append(entry)
        return out
    except (sqlite3.Error, ValueError):
        return []
    finally:
        con.close()


def journal_session(path):
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='session'").fetchone()
            return row[0] if row else ""
        finally:
            con.close()
    except sqlite3.Error:
        return ""


def check_journal(project_file, temp_dir=None):
    """The newest journal left for this project by a crash or a power cut (with edits in it), or None.

    The project file's date says nothing here: saving the project does not save layer edits, so a journal older
    than the project file can still hold edits that were never saved. A journal only disappears when its edits were
    saved or discarded, or when the user answered the offer."""
    for path in journals_left(project_file, temp_dir):
        if journal_session(path) != SESSION and read_journal(path):
            return path
    return None


def discard_left(project_file, temp_dir=None):
    """Remove every journal of this project left by earlier sessions (after the user answered)."""
    for p in journals_left(project_file, temp_dir):
        remove_file(p)


def summary(entries):
    """(layers, new, changed, deleted) counts of a read journal."""
    return (len(entries), sum(len(e["added"]) for e in entries), sum(len(e["changed"]) for e in entries),
            sum(len(e["deleted"]) for e in entries))


def details_text(entries):
    from .i18n import tr

    lines = []
    for e in entries:
        lines.append(tr("{name}: {a} new, {c} changed, {d} deleted features").format(
            name=e["name"], a=len(e["added"]), c=len(e["changed"]), d=len(e["deleted"])))
        if e["added_fields"] or e["deleted_fields"]:
            lines.append("   " + tr("fields added: {a}; fields removed: {d}").format(
                a=", ".join(f["name"] for f in e["added_fields"]) or "-", d=", ".join(e["deleted_fields"]) or "-"))
    return "\n".join(lines)


# ---- writing ------------------------------------------------------------------------------------------------
class _LayerState:
    def __init__(self):
        self.fids = set()        # features touched since the last write
        self.fields = False      # field list changed since the last write
        self.full = True         # rewrite everything (first write, or after a relocation)
        self.base = None         # source state when the edits started


class EditJournal(QObject):
    """Watches the layers being edited and keeps their unsaved edits in the journal file."""

    def __init__(self, temp_dir=None, quiet_ms=QUIET_MS, max_wait=MAX_WAIT_S):
        super().__init__()
        self.temp_dir = temp_dir
        self.max_wait = max_wait
        self.states = {}           # layer id -> _LayerState
        self.hooked = {}           # layer id -> [(signal, slot)]
        self.file = None           # JournalFile written in this session
        self.written = set()       # journal paths this session created
        self.first_dirty = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(quiet_ms)
        self.timer.timeout.connect(self._timed_flush)
        self.last_flush_ms = 0.0
        self.on = enabled()        # read once: _touch runs for every single edit
        self._installed = False

    # ---- lifecycle --------------------------------------------------------
    def set_on(self, on):
        set_enabled(on)
        self.on = bool(on)
        if not on:
            self._forget()

    def install(self):
        prj = QgsProject.instance()
        prj.layersAdded.connect(self._on_layers_added)
        prj.layerWillBeRemoved.connect(self._on_layer_removed)
        prj.projectSaved.connect(self._on_saved)
        prj.cleared.connect(self._on_cleared)
        self._on_layers_added(list(prj.mapLayers().values()))
        self._installed = True
        return self

    def uninstall(self):
        self.timer.stop()
        prj = QgsProject.instance()
        for sig, slot in ((prj.layersAdded, self._on_layers_added), (prj.layerWillBeRemoved, self._on_layer_removed),
                          (prj.projectSaved, self._on_saved), (prj.cleared, self._on_cleared)):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass
        for lid in list(self.hooked):
            self._unhook(lid)
        if self._any_editing():
            self.flush()        # e.g. the plugin is reloaded while editing: keep what is pending
            if self.file is not None:
                self.file.close()
        else:
            self._forget()      # a normal exit: QGIS made the user save or discard the edits first
        self._installed = False

    def _any_editing(self):
        return any(journaled(l) and l.isEditable() and l.isModified() for l in QgsProject.instance().mapLayers().values())

    # ---- layers ---------------------------------------------------------------
    def _on_layers_added(self, layers):
        for lyr in layers:
            if journaled(lyr) and lyr.id() not in self.hooked:
                self._hook(lyr)
                if lyr.isEditable() and lyr.isModified():
                    self._touch(lyr, full=True)

    def _hook(self, lyr):
        lid = lyr.id()
        pairs = [
            (lyr.featureAdded, lambda fid, l=lyr: self._touch(l, fid)),
            (lyr.featureDeleted, lambda fid, l=lyr: self._touch(l, fid)),
            (lyr.geometryChanged, lambda fid, g, l=lyr: self._touch(l, fid)),
            (lyr.attributeValueChanged, lambda fid, i, v, l=lyr: self._touch(l, fid)),
            (lyr.attributeAdded, lambda i, l=lyr: self._touch(l, fields=True)),
            (lyr.attributeDeleted, lambda i, l=lyr: self._touch(l, fields=True)),
            (lyr.editingStarted, lambda l=lyr: self._started(l)),
            (lyr.afterCommitChanges, lambda l=lyr: self._ended(l)),
            (lyr.afterRollBack, lambda l=lyr: self._ended(l)),
            (lyr.editingStopped, lambda l=lyr: self._ended(l)),
        ]
        for sig, slot in pairs:
            sig.connect(slot)
        self.hooked[lid] = pairs

    def _unhook(self, lid):
        for sig, slot in self.hooked.pop(lid, []):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _on_layer_removed(self, lid):
        self._unhook(lid)
        self.states.pop(lid, None)
        if self.file is not None and self.file.con is not None:
            self.file.drop_layer(lid)
            self._delete_if_empty()

    def _started(self, lyr):
        st = self.states.setdefault(lyr.id(), _LayerState())
        st.base = source_state(lyr)

    def _ended(self, lyr):
        """Edits saved or thrown away: nothing of this layer is left to recover."""
        self.states.pop(lyr.id(), None)
        if self.file is not None and self.file.con is not None:
            self.file.drop_layer(lyr.id())
            self._delete_if_empty()

    def _touch(self, lyr, fid=None, fields=False, full=False):
        if not self.on or not lyr.isEditable():
            return
        st = self.states.setdefault(lyr.id(), _LayerState())
        if st.base is None:
            st.base = source_state(lyr)
        if fid is not None:
            st.fids.add(int(fid))
        st.fields = st.fields or fields
        st.full = st.full or full
        now = time.monotonic()
        if self.first_dirty is None:
            self.first_dirty = now
        if now - self.first_dirty >= self.max_wait:
            self.flush()
        else:
            self.timer.start()

    # ---- writing ----------------------------------------------------------------
    def current_path(self):
        return journal_path(QgsProject.instance().fileName(), self.temp_dir)

    def _timed_flush(self):
        """The timer's write: big buffers go in steps, so the window never freezes for long."""
        self.flush(limit=CHUNK)
        if any(st.fids or st.full for st in self.states.values()):
            QTimer.singleShot(0, self._timed_flush)

    def flush(self, limit=None):
        """Write the touched part of every edited layer (at most ``limit`` features per layer). Returns the
        seconds it took."""
        self.timer.stop()
        self.first_dirty = None
        t0 = time.perf_counter()
        prj = QgsProject.instance()
        todo = [(prj.mapLayer(lid), st) for lid, st in self.states.items()
                if st.full or st.fids or st.fields]
        todo = [(l, st) for l, st in todo if l is not None and journaled(l) and l.isEditable()]
        if not todo:
            return 0.0
        path = self.current_path()
        if self.file is None or self.file.path != path:
            if self.file is not None:
                self.file.close()
            self.file = JournalFile(path)
        con = self.file.open()
        self.written.add(path)
        with con:
            for lyr, st in todo:
                self._write_layer(con, lyr, st, limit)
        self.last_flush_ms = (time.perf_counter() - t0) * 1000
        return self.last_flush_ms / 1000

    def _write_layer(self, con, lyr, st, limit=None):
        """Write the touched features of one layer (at most ``limit`` of them; the rest stay for the next write)."""
        buf = lyr.editBuffer()
        lid = lyr.id()
        if buf is None:
            return
        if st.full:
            con.execute("DELETE FROM feats WHERE layer_id=?", (lid,))
            st.fids |= set(buf.addedFeatures().keys()) | set(buf.deletedFeatureIds()) | \
                set(buf.changedGeometries().keys()) | set(buf.changedAttributeValues().keys())
            st.full = False
        if limit is not None and len(st.fids) > limit:
            fids = set(sorted(st.fids)[:limit])
        else:
            fids = set(st.fids)
        pfields = lyr.dataProvider().fields()
        added_fields = [_field_dict(f) for f in buf.addedAttributes()]
        deleted_fields = [pfields.at(i).name() for i in buf.deletedAttributeIds() if 0 <= i < pfields.count()]
        con.execute("INSERT OR REPLACE INTO layers VALUES(?,?,?,?,?,?,?,?,?)",
                    (lid, lyr.name(), lyr.source(), lyr.providerType(), lyr.crs().authid(),
                     json.dumps(st.base or {}), json.dumps(added_fields), json.dumps(deleted_fields), time.time()))
        names = lyr.fields().names()
        rows, gone = [], []
        changed_values = None   # copied once: the buffer hands out a full copy on every call
        current = {}
        if len(fids) > 50:   # one request for the whole step instead of one per feature
            from qgis.core import QgsFeatureRequest

            current = {f.id(): f for f in lyr.getFeatures(QgsFeatureRequest().setFilterFids(list(fids)))}
        for fid in fids:
            if buf.isFeatureAdded(fid):
                f = current.get(fid) if current else lyr.getFeature(fid)
                if f is None or not f.isValid():
                    gone.append((lid, fid))
                    continue
                attrs = {n: encode(f.attribute(i)) for i, n in enumerate(names)}
                rows.append((lid, fid, "add", _geom_bytes(f.geometry()), json.dumps(attrs)))
            elif buf.isFeatureDeleted(fid):
                rows.append((lid, fid, "del", None, None))
            elif buf.isFeatureGeometryChanged(fid) or buf.isFeatureAttributesChanged(fid):
                geom = None
                if buf.isFeatureGeometryChanged(fid):
                    f = current.get(fid) or lyr.getFeature(fid)
                    geom = _geom_bytes(f.geometry())
                attrs = {}
                if buf.isFeatureAttributesChanged(fid):
                    if changed_values is None:
                        changed_values = buf.changedAttributeValues()
                    changed = changed_values.get(fid, {})
                    attrs = {names[i]: encode(v) for i, v in changed.items() if 0 <= i < len(names)}
                rows.append((lid, fid, "chg", geom, json.dumps(attrs)))
            else:
                gone.append((lid, fid))   # undone
        if gone:
            con.executemany("DELETE FROM feats WHERE layer_id=? AND fid=?", gone)
        if rows:
            con.executemany("INSERT OR REPLACE INTO feats VALUES(?,?,?,?,?)", rows)
        st.fids -= fids
        st.fields = False

    def _delete_if_empty(self):
        if self.file is not None and self.file.is_empty():
            self.file.delete()
            self.written.discard(self.file.path)
            self.file = None

    def _forget(self):
        """Remove the journals this session wrote."""
        if self.file is not None:
            self.file.close()
        for p in self.written:
            remove_file(p)
        self.written.clear()
        self.file = None
        self.states.clear()

    # ---- project events ---------------------------------------------------------
    def _on_saved(self):
        """A saved project with no layer in edit mode has nothing left to recover. A project saved under a new
        name (a temporary map that became a project folder) moves the journal next to it."""
        if not self._any_editing():
            self._forget()
            return
        if self.file is not None and self.file.path != self.current_path():
            old = self.file.path
            self.file.close()
            remove_file(old)
            self.written.discard(old)
            self.file = None
            for st in self.states.values():
                st.full = True
            self.flush()

    def _on_cleared(self):
        """Closed the normal way (QGIS asked about unsaved edits first)."""
        self.timer.stop()
        self.first_dirty = None
        for lid in list(self.hooked):
            self._unhook(lid)
        self._forget()


# ---- restoring -------------------------------------------------------------------------------------------------
def _match(entry, prj):
    layers = [l for l in prj.mapLayers().values() if isinstance(l, QgsVectorLayer)]
    src = _norm(entry["source"])
    for l in layers:
        if _norm(l.source()) == src and l.providerType() == entry["provider"]:
            return l
    l = prj.mapLayer(entry["layer_id"])
    if isinstance(l, QgsVectorLayer):
        return l
    same = [l for l in layers if l.name() == entry["name"] and l.providerType() == entry["provider"]]
    return same[0] if len(same) == 1 else None


def source_changed(entry, lyr):
    """True when the layer's data changed outside this edit session (another program, another computer)."""
    base, now = entry.get("base") or {}, source_state(lyr)
    if base.get("count", -1) >= 0 and now["count"] >= 0 and base["count"] != now["count"]:
        return True
    if base.get("size") is not None and now["size"] is not None and not now["path"].lower().endswith(".gpkg"):
        return base["size"] != now["size"] and base.get("mtime") != now["mtime"]
    return False


def restore(path, prj=None):
    """Put the edits of a journal back into the layers' edit buffers (unsaved, one undo step per layer).

    Returns a report: {"layers": [(name, restored counts dict)], "skipped": [texts], "missing": [layer names]}."""
    from .i18n import tr

    prj = prj or QgsProject.instance()
    entries = read_journal(path)
    report = {"layers": [], "skipped": [], "missing": []}
    for e in entries:
        lyr = _match(e, prj)
        if lyr is None or not lyr.isValid():
            report["missing"].append(e["name"])
            continue
        changed_outside = source_changed(e, lyr)
        if not lyr.isEditable() and not lyr.startEditing():
            report["skipped"].append(tr("{name}: the layer cannot be edited").format(name=lyr.name()))
            continue
        done = {"added": 0, "changed": 0, "deleted": 0, "skipped": 0}
        lyr.beginEditCommand(tr("Restore unsaved edits"))
        try:
            have = set(lyr.fields().names())
            for fd in e["added_fields"]:
                if fd["name"] not in have:
                    lyr.addAttribute(_field_from(fd))
            for name in e["deleted_fields"]:
                i = lyr.fields().indexOf(name)
                if i >= 0:
                    lyr.deleteAttribute(i)
            names = lyr.fields().names()

            def exists(fid):
                return lyr.getFeature(fid).isValid()

            dels = [r["fid"] for r in e["deleted"] if exists(r["fid"])]
            done["skipped"] += len(e["deleted"]) - len(dels)
            if dels:
                lyr.deleteFeatures(dels)
                done["deleted"] = len(dels)
            for r in e["changed"]:
                if not exists(r["fid"]):
                    done["skipped"] += 1
                    continue
                if r["geom"] is not None:
                    g = QgsGeometry()
                    g.fromWkb(r["geom"])
                    lyr.changeGeometry(r["fid"], g)
                vals = {names.index(k): decode(v) for k, v in r["attrs"].items() if k in names}
                if vals:
                    lyr.changeAttributeValues(r["fid"], vals)
                done["changed"] += 1
            feats = []
            fields = lyr.fields()
            pks = set(lyr.dataProvider().pkAttributeIndexes())   # e.g. a GeoPackage "fid": the data source sets it
            for r in e["added"]:
                f = QgsFeature(fields)
                for k, v in r["attrs"].items():
                    i = fields.indexOf(k)
                    if i >= 0 and i not in pks:
                        f.setAttribute(i, decode(v))
                if r["geom"] is not None:
                    g = QgsGeometry()
                    g.fromWkb(r["geom"])
                    f.setGeometry(g)
                feats.append(f)
            if feats:
                lyr.addFeatures(feats)
                done["added"] = len(feats)
        finally:
            lyr.endEditCommand()
        lyr.triggerRepaint()
        if changed_outside:
            report["skipped"].append(tr("{name}: the data was changed by another program since these edits were "
                                        "made - check them before saving").format(name=lyr.name()))
        if done["skipped"]:
            report["skipped"].append(tr("{name}: {n} edit(s) were skipped because their features no longer "
                                        "exist").format(name=lyr.name(), n=done["skipped"]))
        report["layers"].append((lyr.name(), done))
    return report


def report_text(report):
    from .i18n import tr

    lines = [tr("{name}: {a} new, {c} changed, {d} deleted features put back").format(
        name=n, a=d["added"], c=d["changed"], d=d["deleted"]) for n, d in report["layers"]]
    lines += report["skipped"]
    if report["missing"]:
        lines.append(tr("Layers not found in the project: {names}").format(names=", ".join(report["missing"])))
    return "\n".join(lines)
