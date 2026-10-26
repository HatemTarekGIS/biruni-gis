"""Project activity log.

Records what is done in a project - working sessions and active time, layers
added/removed, features added / reshaped / edited / deleted per layer, tools
run, saves - and stores it inside the project:

  * project folder  -> tables in <Name>.gpkg
  * other projects  -> <project>.activity.sqlite next to the project file
  * temporary maps  -> kept in memory and written when the map is saved

Active time counts the minutes in which the user did something (editing,
navigating the map, running tools); idle minutes are not counted.
"""
import datetime as dt
import os
import socket
import sqlite3
import uuid

from qgis.core import QgsApplication, QgsProject, QgsSettings, QgsVectorLayer
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtWidgets import QApplication

from .version import APP_NAME, VERSION

ENABLED_KEY = "biruni/activity/enabled"

SCHEMA = """
CREATE TABLE IF NOT EXISTS biruni_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, session TEXT, user TEXT, machine TEXT,
    category TEXT, action TEXT, target TEXT, target_id TEXT, count INTEGER, details TEXT);
CREATE TABLE IF NOT EXISTS biruni_sessions (
    session TEXT PRIMARY KEY, user TEXT, machine TEXT, started TEXT, ended TEXT,
    active_minutes INTEGER, app TEXT);
CREATE TABLE IF NOT EXISTS biruni_active_time (
    session TEXT, day TEXT, minutes INTEGER, PRIMARY KEY (session, day));
"""

# edit actions and their meaning, in report order
EDIT_ACTIONS = ("features_added", "geometries_changed", "attributes_changed", "features_deleted")


def now():
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def storage_path(prj=None):
    """Where the current project's activity is stored (None for temporary maps)."""
    from .project import project_gpkg

    prj = prj or QgsProject.instance()
    gpkg = project_gpkg(prj)
    if gpkg:
        return gpkg
    if prj.fileName():
        base = os.path.splitext(prj.fileName())[0]
        return base + ".activity.sqlite"
    return None


class ActivityStore:
    """Plain-SQLite access (a GeoPackage is an SQLite database)."""

    def __init__(self, path):
        self.path = path

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.executescript(SCHEMA)
        return con

    def write(self, events, session_row, minutes):
        con = self._connect()
        try:
            with con:
                con.executemany(
                    "INSERT INTO biruni_activity (ts, session, user, machine, category, action, target, "
                    "target_id, count, details) VALUES (?,?,?,?,?,?,?,?,?,?)", events)
                if session_row:
                    con.execute("INSERT OR REPLACE INTO biruni_sessions VALUES (?,?,?,?,?,?,?)", session_row)
                for (session, day), m in minutes.items():
                    con.execute("INSERT INTO biruni_active_time VALUES (?,?,?) ON CONFLICT(session, day) "
                                "DO UPDATE SET minutes = excluded.minutes", (session, day, m))
        finally:
            con.close()

    def read(self):
        if not self.path or not os.path.exists(self.path):
            return [], [], []
        con = self._connect()
        try:
            con.row_factory = sqlite3.Row
            events = [dict(r) for r in con.execute("SELECT * FROM biruni_activity ORDER BY ts, id")]
            sessions = [dict(r) for r in con.execute("SELECT * FROM biruni_sessions ORDER BY started")]
            active = [dict(r) for r in con.execute("SELECT * FROM biruni_active_time ORDER BY day")]
            return events, sessions, active
        finally:
            con.close()


class ActivityLogger(QObject):
    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.iface = plugin.iface
        self.user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
        self.machine = os.environ.get("COMPUTERNAME") or socket.gethostname()
        self.pending = []          # events not yet written
        self.minutes = {}          # (session, day) -> active minutes (cumulative)
        self.session = None
        self.started = None
        self.active_minutes = 0
        self._storage = None
        self._touched = False
        self._hooked = set()
        self._timer = QTimer(self)
        self._timer.setInterval(60_000)
        self._timer.timeout.connect(self._tick)

    # ---- settings ------------------------------------------------------
    @staticmethod
    def enabled():
        return QgsSettings().value(ENABLED_KEY, True, type=bool)

    @staticmethod
    def set_enabled(on):
        QgsSettings().setValue(ENABLED_KEY, bool(on))

    # ---- lifecycle -----------------------------------------------------
    def install(self):
        prj = QgsProject.instance()
        prj.readProject.connect(self._on_read)
        prj.projectSaved.connect(self._on_saved)
        prj.cleared.connect(self._on_cleared)
        prj.layersAdded.connect(self._on_layers_added)
        prj.layersWillBeRemoved.connect(self._on_layers_removed)
        self.iface.mapCanvas().extentsChanged.connect(self.touch)
        try:
            from qgis.gui import QgsGui

            QgsGui.historyProviderRegistry().entryAdded.connect(self._on_history)
        except Exception:
            pass
        QApplication.instance().aboutToQuit.connect(self._on_quit)
        self._timer.start()
        for lyr in prj.mapLayers().values():
            self._hook(lyr)

    def unload(self):
        self._end_session()
        self._timer.stop()
        prj = QgsProject.instance()
        for sig, slot in ((prj.readProject, self._on_read), (prj.projectSaved, self._on_saved),
                          (prj.cleared, self._on_cleared), (prj.layersAdded, self._on_layers_added),
                          (prj.layersWillBeRemoved, self._on_layers_removed),
                          (self.iface.mapCanvas().extentsChanged, self.touch),
                          (QApplication.instance().aboutToQuit, self._on_quit)):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass

    # ---- recording -----------------------------------------------------
    def touch(self, *args):
        self._touched = True

    def log(self, category, action, target="", target_id="", count=1, details=""):
        if not self.enabled():
            return
        if self.session is None:
            self._start_session()
        self.touch()
        self.pending.append((now(), self.session, self.user, self.machine, category, action,
                             target or "", target_id or "", int(count or 0), details or ""))

    def _start_session(self):
        self.session = uuid.uuid4().hex[:12]
        self.started = now()
        self.active_minutes = 0
        self._storage = storage_path()
        self.pending.append((self.started, self.session, self.user, self.machine, "session", "start",
                             QgsProject.instance().title() or "", "", 0, f"{APP_NAME} {VERSION}"))

    def _end_session(self):
        if self.session is None:
            return
        self.pending.append((now(), self.session, self.user, self.machine, "session", "end", "", "", 0, ""))
        self.flush(ended=True)
        self.session = None

    def _tick(self):
        if self._touched and self.session is not None and self.enabled():
            self.active_minutes += 1
            key = (self.session, dt.date.today().isoformat())
            self.minutes[key] = self.minutes.get(key, 0) + 1
        self._touched = False
        self.flush()

    def flush(self, ended=False):
        """Write pending events to the project's storage (if it has one)."""
        if self.session is None:
            return
        path = self._storage or storage_path()
        if not path:
            return  # temporary map: keep everything in memory until it is saved
        self._storage = path
        row = (self.session, self.user, self.machine, self.started, now() if ended else None,
               self.active_minutes, f"{APP_NAME} {VERSION}")
        try:
            ActivityStore(path).write(self.pending, row, self.minutes)
            self.pending = []
        except sqlite3.Error:
            pass  # e.g. database busy - retried at the next tick

    def snapshot(self):
        """Stored + not-yet-written records of the current project."""
        self.flush()
        events, sessions, active = ActivityStore(self._storage or storage_path()).read()
        if self.pending:  # temporary map, nothing stored yet
            cols = ("ts", "session", "user", "machine", "category", "action", "target", "target_id",
                    "count", "details")
            events += [dict(zip(cols, e)) for e in self.pending]
        if self.session and not any(s["session"] == self.session for s in sessions):
            sessions.append({"session": self.session, "user": self.user, "machine": self.machine,
                             "started": self.started, "ended": None, "active_minutes": self.active_minutes,
                             "app": f"{APP_NAME} {VERSION}"})
        known = {(a["session"], a["day"]) for a in active}
        active += [{"session": s, "day": d, "minutes": m} for (s, d), m in self.minutes.items()
                   if (s, d) not in known]
        return events, sessions, active

    # ---- project events -----------------------------------------------
    def _on_read(self, *args):
        self._end_session()
        self.minutes = {}
        self._start_session()
        self.log("project", "opened", QgsProject.instance().title() or os.path.basename(
            QgsProject.instance().fileName()))
        for lyr in QgsProject.instance().mapLayers().values():
            self._hook(lyr)
        self.flush()

    def _on_saved(self):
        if self.session is not None and not self._storage:
            self._storage = storage_path()  # temporary map just became a project
        self.log("project", "saved", os.path.basename(QgsProject.instance().fileName()))
        self.flush()

    def _on_cleared(self):
        self._end_session()
        self.minutes = {}
        self._storage = None
        self._hooked = set()

    def _on_quit(self):
        self._end_session()

    def _on_layers_added(self, layers):
        for lyr in layers:
            self._hook(lyr)
            fmt = lyr.providerType()
            if isinstance(lyr, QgsVectorLayer):
                fmt = f"{lyr.providerType()} · {lyr.storageType()}"
            self.log("layer", "added", lyr.name(), lyr.id(), 1, fmt)

    def _on_layers_removed(self, ids):
        prj = QgsProject.instance()
        for lid in ids:
            lyr = prj.mapLayer(lid)
            self.log("layer", "removed", lyr.name() if lyr else "", lid, 1)

    def _hook(self, lyr):
        if not isinstance(lyr, QgsVectorLayer) or lyr.id() in self._hooked:
            return
        self._hooked.add(lyr.id())
        lyr.committedFeaturesAdded.connect(
            lambda lid, feats, l=lyr: self.log("edit", "features_added", l.name(), lid, len(feats)))
        lyr.committedFeaturesRemoved.connect(
            lambda lid, fids, l=lyr: self.log("edit", "features_deleted", l.name(), lid, len(fids)))
        lyr.committedGeometriesChanges.connect(
            lambda lid, geoms, l=lyr: self.log("edit", "geometries_changed", l.name(), lid, len(geoms)))
        lyr.committedAttributeValuesChanges.connect(
            lambda lid, vals, l=lyr: self.log("edit", "attributes_changed", l.name(), lid, len(vals)))
        lyr.committedAttributesAdded.connect(
            lambda lid, fields, l=lyr: self.log("edit", "fields_added", l.name(), lid, len(fields)))
        lyr.committedAttributesDeleted.connect(
            lambda lid, idx, l=lyr: self.log("edit", "fields_deleted", l.name(), lid, len(idx)))
        lyr.afterRollBack.connect(lambda l=lyr: self.log("edit", "edits_discarded", l.name(), l.id(), 1))
        lyr.editingStarted.connect(lambda l=lyr: self.log("edit", "editing_started", l.name(), l.id(), 1))
        # uncommitted work still counts as active time
        lyr.featureAdded.connect(self.touch)
        lyr.geometryChanged.connect(self.touch)
        lyr.attributeValueChanged.connect(self.touch)

    def _on_history(self, entry_id, entry, *args):
        try:
            if entry.providerId != "processing":
                return
            data = entry.entry or {}
            alg_id = data.get("algorithm_id", "")
            alg = QgsApplication.processingRegistry().algorithmById(alg_id) if alg_id else None
            self.log("tool", "run", alg.displayName() if alg else alg_id, alg_id, 1)
        except Exception:
            pass
