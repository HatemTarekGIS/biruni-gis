"""When a backup job is due, and the timer that runs one at a time (only while Biruni is open: no Windows Task Scheduler).

``due`` is a pure function. The Scheduler never blocks the window: each job runs in the background helper's thread and
its result comes back on the main thread. It never schedules forget or prune.
"""
import os
import time

TICK_MS = 10 * 60 * 1000
HOUR, DAY = 3600.0, 86400.0
RETRY_S = HOUR
ORDER = ("backup", "check", "restore", "vault")     # check and restore need a repository that has a backup in it


def due(state, cfg, now, present=True):
    """The jobs that are due now, in the order they should run. [] when backups are off or the target is absent
    (the run is then deferred and caught up when the disk is back)."""
    if not cfg.get("enabled") or not cfg.get("repo") or not present:
        return []
    state = state or {}

    def age(key):
        at = (state.get(key) or {}).get("at")
        return None if not isinstance(at, (int, float)) else now - at

    out = []
    a = age("last_backup")
    if a is None or a >= cfg.get("backup-hours", 24) * HOUR:
        out.append("backup")
    if a is not None or state.get("last_backup"):
        c = age("last_check")
        if c is None or c >= cfg.get("check-days", 7) * DAY:
            out.append("check")
        rs = age("last_restore")
        if rs is None or rs >= cfg.get("restore-days", 30) * DAY:
            out.append("restore")
    v = age("last_vault")
    if v is None or v >= cfg.get("vault-days", 7) * DAY:
        out.append("vault")
    return out


class Scheduler:
    """``engine_factory()`` builds an engine.Engine (a fresh one each time, so settings changes count);
    ``runner(work, done)`` is background.run (tests pass a synchronous one)."""

    def __init__(self, engine_factory, get_setting, cert_folder, runner=None, on_result=None, now=time.time):
        self.factory, self.get, self.cert = engine_factory, get_setting, cert_folder
        self.runner, self.on_result, self.now = runner, on_result, now
        self.running = None
        self.timer = None
        self.calls = []
        self.tried = {}

    def start(self):
        from qgis.PyQt.QtCore import QTimer

        if self.timer is None:
            self.timer = QTimer()
            self.timer.setInterval(TICK_MS)
            self.timer.timeout.connect(self.tick)
        self.timer.start()
        QTimer.singleShot(20000, self.tick)     # the first look soon after start-up, not during it

    def stop(self):
        if self.timer is not None:
            self.timer.stop()

    def tick(self):
        """Start the first due job when none is running. Returns its name, or None."""
        from . import engine

        if self.running:
            return None
        cfg = engine.config(self.get)
        present = engine.target_present(cfg["repo"]) if cfg["repo"] else False
        jobs = due(engine.load_state(self.cert), cfg, self.now(), present)
        if cfg["enabled"] and cfg["repo"] and not present:
            eng = self.factory()
            eng.run_backup()          # records the deferral (no process is started)
            return None
        jobs = [j for j in jobs if self.now() - self.tried.get(j, -1e12) >= RETRY_S]   # a failing job is not hammered
        if not jobs:
            return None
        job = jobs[0]
        self.tried[job] = self.now()
        self.running = job
        eng = self.factory()
        fn = {"backup": eng.run_backup, "check": eng.run_check, "restore": eng.run_restore_test,
              "vault": eng.run_vault_snapshot}[job]
        self.calls.append(job)

        def done(res, err):
            self.running = None
            if self.on_result is not None:
                self.on_result(job, res, err)

        runner = self.runner
        if runner is None:
            from .. import background

            runner = background.run
        runner(fn, done)
        return job


# ---- wiring to the running plugin ---------------------------------------------------------------------------------------
_SCHEDULERS = {}


def make_engine(plugin=None):
    """An engine.Engine over this PC's live settings. When the field-photos window has a server running, its own
    ledger is used, so a `backup` mark is seen at once by the phone-delete rule."""
    import sys

    from qgis.core import QgsApplication, QgsSettings

    from . import engine

    s = QgsSettings()
    cert = os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")
    from ..field_photos.dock import KEY as FP_KEY, default_folder

    photos = s.value(FP_KEY + "folder", "") or default_folder()
    ledger = None
    fp = sys.modules.get(__package__.rsplit(".", 1)[0] + ".field_photos.dock")
    d = getattr(fp, "_DOCK", {}).get(id(plugin)) if fp is not None else None
    store = getattr(getattr(d, "server", None), "store", None)
    if store is not None and getattr(store, "folder", "") == photos:
        ledger = store.ledger
    from ..field_photos import archive

    try:
        arch = archive.get_archive_root(photos)
    except Exception:  # noqa: BLE001
        arch = ""
    try:
        from ..team_vault.core import vault_root

        vroot = vault_root()
    except Exception:  # noqa: BLE001
        vroot = ""
    # the settings are read here, on the main thread; the job's thread only sees this copy
    snap = {k: s.value(k, None) for k in engine.setting_keys()}
    return engine.Engine(cert, photos, lambda k, d=None: snap.get(k) if snap.get(k) is not None else d, ledger=ledger,
                         archive_root=arch, vault_root=vroot)


def start(plugin):
    """Start the 10-minute look (a no-op until a backup disk is chosen) and the automatic intake of a backup PC."""
    from qgis.core import QgsApplication, QgsSettings

    s = QgsSettings()
    cert = os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")
    sch = _SCHEDULERS.get(id(plugin))
    if sch is None:
        sch = Scheduler(lambda: make_engine(plugin), lambda k, d=None: s.value(k, d), cert)
        _SCHEDULERS[id(plugin)] = sch
    sch.start()
    try:
        from ..field_photos import backup_intake

        backup_intake.autostart(plugin)
    except Exception:  # noqa: BLE001 - the intake is optional; the health row says when it is not running
        pass
    return sch


def stop(plugin):
    sch = _SCHEDULERS.pop(id(plugin), None)
    if sch is not None:
        sch.stop()
    try:
        from ..field_photos import backup_intake

        backup_intake.stop(plugin)
    except Exception:  # noqa: BLE001
        pass
