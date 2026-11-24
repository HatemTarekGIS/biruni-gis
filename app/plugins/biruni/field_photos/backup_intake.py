"""A backup PC takes in the office's photos and positions by itself (no-loss P9, stage 9b).

With the office pack (office-pack.bin, kept by the "Backup PC" window) this computer knows the office's bridge keys and
surveyor codes. For each bridge key it runs one DriveIntake (the .bfp photos in the office's Google Drive folder) and one
PosIntake (the .bgp positions). It is read-only on Drive: nothing is trashed, no receipt is written, no file is made
there. Photos go to this PC's own PhotoStore like any photo that came over the bridge.

Starts from plugin.py when office-pack.bin exists and `biruni/backup/auto-intake` is on.
"""
import base64
import hashlib
import json
import os
import threading
import time

PACK_FILE = "office-pack.bin"
AUTO_SETTING = "biruni/backup/auto-intake"
DRIVE_ROOTS = "biruni/field-photos/drive-roots"
STATE_FILE = "intake.json"


def load_pack(cert_folder, handover_key):
    """The office pack kept on this computer -> {"keys", "surveyors", ...}, or None (no pack, or not for this PC)."""
    from . import recipients
    from .tunnel import unprotect

    try:
        with open(os.path.join(cert_folder, PACK_FILE), encoding="ascii") as f:
            blob = base64.b64decode(unprotect(f.read().strip()) or "")
        return recipients.open_office_pack(handover_key, blob)
    except (OSError, ValueError):
        return None


class _Events:
    """What DriveIntake reports: counted, never queued (nobody drains a queue on a backup PC)."""

    def __init__(self):
        self.new = 0
        self.rejected = 0
        self.last_error = ""

    def put(self, ev):
        try:
            kind, data = ev
        except (TypeError, ValueError):
            return
        if kind == "photo":
            self.new += 1
        elif kind == "rejected":
            self.rejected += 1
        elif kind == "drive" and isinstance(data, dict) and data.get("error"):
            self.last_error = str(data["error"])[:200]


class BackupIntake:
    def __init__(self, roots, store, handover, pack, events=None, state_folder="", interval=None):
        from . import drive

        self.store, self.pack = store, pack
        self.events = events if events is not None else _Events()
        self.state_folder = state_folder
        self.roots = list(roots or ())
        self.keys = [k for k in ((pack or {}).get("keys") or {}).values() if isinstance(k, str) and len(k) == 43]
        surveyors = lambda: (self.pack or {}).get("surveyors") or {"qr_mode": "shared", "surveyors": {}}   # noqa: E731
        iv = interval if interval is not None else drive.INTERVAL
        self.drives = [drive.DriveIntake(self.roots, store, handover, k, self.events, interval=iv, surveyors=surveyors)
                       for k in self.keys]
        self.pos = [drive.PosIntake(self.roots, k, self._pos_taker(k), interval=iv) for k in self.keys]
        self.opened = 0
        self._adopt_surveyors()
        self._stop = threading.Event()
        self.thread = None

    def _adopt_surveyors(self):
        """PhotoStore.save knows a surveyor only through the surveyor list in its own folder. A backup PC that has none
        takes the office pack's list (so photos carry the surveyor's name); one that has its own list keeps it."""
        from .server import SURVEYOR_KEYS_NAME, write_surveyor_keys

        s = (self.pack or {}).get("surveyors")
        if not isinstance(s, dict) or not s.get("surveyors"):
            return
        if os.path.isfile(os.path.join(self.store.folder, SURVEYOR_KEYS_NAME)):
            return
        try:
            write_surveyor_keys(self.store.folder, s)
        except OSError:
            pass

    def _pos_taker(self, k):
        """take(sealed, via) for PosIntake: a position file this office's key opens is kept (once) in
        <photo folder>\\positions\\ so the backup PC holds the same positions; True when kept."""
        from .ladder import Ladder

        lad = Ladder(k)

        def take(sealed, _via):
            if not isinstance(lad.open(sealed), dict):
                return False
            name = "pos-" + hashlib.sha256(sealed.encode("utf-8")).hexdigest()[:24] + ".lpos"
            d = os.path.join(self.store.folder, "positions")
            try:
                os.makedirs(d, exist_ok=True)
                p = os.path.join(d, name)
                if not os.path.exists(p):
                    with open(p + ".part", "w", encoding="ascii") as f:
                        f.write(sealed)
                    os.replace(p + ".part", p)
            except OSError:
                return False
            self.opened += 1
            return True

        return take

    def refused(self):
        """Files refused and not read again (damaged, another office, a revoked surveyor...)."""
        return sum(1 for d in self.drives for v in list(d.seen.values()) if (v or [0, 0, 0])[2:] == [0])

    def scan_once(self):
        """One pass of every intake; returns {"new", "positions", "refused"} and writes intake.json."""
        new = 0
        for d in self.drives:
            new += d.scan().get("new", 0)
        pos = sum(p.scan() for p in self.pos)
        out = {"new": new, "positions": pos, "refused": self.refused()}
        self._write_state(out)
        return out

    def _write_state(self, out):
        if not self.state_folder:
            return
        d = os.path.join(self.state_folder, "backup")
        try:
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, STATE_FILE)
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                json.dump(dict(out, at=time.time(), error=getattr(self.events, "last_error", "")), f)
            os.replace(p + ".tmp", p)
        except OSError:
            pass

    def start(self):
        if self.thread is None and self.keys:
            self._stop.clear()
            self.thread = threading.Thread(target=self._run, name="biruni-backup-intake", daemon=True)
            self.thread.start()

    def _run(self):
        iv = self.drives[0].interval if self.drives else 15
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as x:  # noqa: BLE001 - keeps going; the health row shows a stale scan
                if hasattr(self.events, "last_error"):
                    self.events.last_error = str(x)[:200]
            self._stop.wait(iv)

    def stop(self):
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None


# ---- started by the plugin -------------------------------------------------------------------------------------------
_RUNNING = {}


def _roots():
    from qgis.core import QgsSettings

    try:
        raw = json.loads(QgsSettings().value(DRIVE_ROOTS, "") or "[]")
    except (TypeError, ValueError):
        return []
    return [r for r in raw if isinstance(r, str) and r] if isinstance(raw, list) else []


def autostart(plugin):
    """Start the intake when this PC has the office pack and the setting is on. Returns the BackupIntake or None."""
    from qgis.core import QgsApplication, QgsSettings

    from .handover import HandoverKey
    from .server import PhotoStore

    if id(plugin) in _RUNNING:
        return _RUNNING[id(plugin)]
    s = QgsSettings()
    on = s.value(AUTO_SETTING, False)
    if not (on if isinstance(on, bool) else str(on).strip().lower() in ("1", "true", "yes", "on")):
        return None
    cert = os.path.join(QgsApplication.qgisSettingsDirPath(), "biruni")
    if not os.path.isfile(os.path.join(cert, PACK_FILE)):
        return None
    hk = HandoverKey(cert)
    pack = load_pack(cert, hk)
    roots = _roots()
    if pack is None or not roots:
        return None
    from .dock import KEY as FP_KEY, default_folder

    photos = s.value(FP_KEY + "folder", "") or default_folder()
    store = PhotoStore(photos)
    bi = BackupIntake(roots, store, hk, pack, state_folder=cert)
    bi.start()
    _RUNNING[id(plugin)] = bi
    return bi


def stop(plugin):
    bi = _RUNNING.pop(id(plugin), None)
    if bi is not None:
        bi.stop()
