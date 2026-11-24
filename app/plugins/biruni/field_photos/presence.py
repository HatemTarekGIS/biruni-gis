"""Persistent surveyor presence & queue status on the office PC (no-loss P4/P5).

Stores `<photos folder>/team_seen.json` atomically so "last seen", queue counts (`q`),
oldest pending item (`old`), position backlog (`lp`), tracking mode (`tm`) and consented
policy sequence (`cs`) survive Biruni restarts and work even when position tracking is off.
"""
import json
import os
import threading
import time

FILE = "team_seen.json"
SAVE_INTERVAL = 30.0
MODES = ("", "off", "photo", "1", "5", "15", "live")


def _clean_row(r):
    if not isinstance(r, dict) or not isinstance(r.get("dev"), str) or not r["dev"]:
        return None
    q = r.get("q")
    if isinstance(q, (list, tuple)) and len(q) == 4:
        q = [max(0, int(x)) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0 for x in q]
    else:
        q = None
    old = r.get("old")
    old = int(old) if isinstance(old, (int, float)) and not isinstance(old, bool) and old > 0 else None
    lp = r.get("lp")
    lp = max(0, int(lp)) if isinstance(lp, (int, float)) and not isinstance(lp, bool) else None
    cs = r.get("cs")
    cs = max(0, int(cs)) if isinstance(cs, (int, float)) and not isinstance(cs, bool) else None
    heard = r.get("heard")
    heard = float(heard) if isinstance(heard, (int, float)) and not isinstance(heard, bool) and heard > 0 else None
    pos_at = r.get("pos_at")
    pos_at = float(pos_at) if isinstance(pos_at, (int, float)) and not isinstance(pos_at, bool) and pos_at > 0 else None
    lat = r.get("lat")
    lat = float(lat) if isinstance(lat, (int, float)) and not isinstance(lat, bool) and -90 <= lat <= 90 else None
    lon = r.get("lon")
    lon = float(lon) if isinstance(lon, (int, float)) and not isinstance(lon, bool) and -180 <= lon <= 180 else None
    acc = r.get("acc")
    acc = float(acc) if isinstance(acc, (int, float)) and not isinstance(acc, bool) and acc >= 0 else None
    tm = str(r.get("tm") or "")
    if tm not in MODES:
        tm = ""
    return {
        "dev": str(r["dev"])[:64],
        "name": str(r.get("name") or "")[:40],
        "heard": heard,
        "pos_at": pos_at,
        "lat": lat,
        "lon": lon,
        "acc": acc,
        "tm": tm,
        "q": q,
        "old": old,
        "lp": lp,
        "cs": cs,
    }


class Presence:
    """Tracks surveyor presence from positions and coordinate-free `hs` status beats."""

    def __init__(self, folder):
        self.folder = str(folder or "")
        self.path = os.path.join(self.folder, FILE) if self.folder else ""
        self._lock = threading.Lock()
        self._rows = {}
        self._dirty = False
        self._last_save = 0.0
        self._load()

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return
        items = doc.get("rows") if isinstance(doc, dict) else (doc if isinstance(doc, list) else None)
        if isinstance(items, dict):
            items = list(items.values())
        if not isinstance(items, list):
            return
        for item in items:
            row = _clean_row(item)
            if row is not None:
                self._rows[row["dev"]] = row

    def _entry(self, dev):
        row = self._rows.get(dev)
        if row is None:
            row = {
                "dev": dev,
                "name": "",
                "heard": None,
                "pos_at": None,
                "lat": None,
                "lon": None,
                "acc": None,
                "tm": "",
                "q": None,
                "old": None,
                "lp": None,
                "cs": None,
            }
            self._rows[dev] = row
        return row

    def note_pos(self, pos, now=None):
        """Record an authenticated position dict from `clean_pos` / `resolve_pos`."""
        if not isinstance(pos, dict):
            return None
        dev = str(pos.get("id") or pos.get("dev") or "").strip()[:64]
        if not dev:
            return None
        now_s = float(time.time() if now is None else now)
        t_ms = pos.get("t")
        pos_s = (float(t_ms) / 1000.0) if isinstance(t_ms, (int, float)) and not isinstance(t_ms, bool) and t_ms > 0 else now_s
        with self._lock:
            row = self._entry(dev)
            name = str(pos.get("name") or "").strip()[:40]
            if name:
                row["name"] = name
            if not pos.get("late"):
                row["heard"] = max(row["heard"] or 0.0, now_s)
            elif row["heard"] is None:
                row["heard"] = pos_s
            if row["pos_at"] is None or pos_s >= row["pos_at"] or not pos.get("late"):
                row["pos_at"] = pos_s
                lat, lon = pos.get("lat"), pos.get("lon")
                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                    row["lat"] = float(lat)
                    row["lon"] = float(lon)
                acc = pos.get("acc")
                if isinstance(acc, (int, float)) and acc >= 0:
                    row["acc"] = float(acc)
            tm = str(pos.get("tm") or "")
            if tm in MODES and tm:
                row["tm"] = tm
            self._dirty = True
            self._maybe_save(now_s)
            return dict(row)

    def note_hs(self, hs, now=None):
        """Record an authenticated status message `hs` from `clean_hs` / `resolve_pos`."""
        if not isinstance(hs, dict):
            return None
        dev = str(hs.get("d") or hs.get("id") or hs.get("dev") or "").strip()[:64]
        if not dev:
            return None
        now_s = float(time.time() if now is None else now)
        with self._lock:
            row = self._entry(dev)
            name = str(hs.get("name") or hs.get("n") or "").strip()[:40]
            if name:
                row["name"] = name
            row["heard"] = max(row["heard"] or 0.0, now_s)
            q = hs.get("q")
            if isinstance(q, (list, tuple)) and len(q) == 4:
                row["q"] = [max(0, int(x)) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0 for x in q]
            if "old" in hs:
                old = hs.get("old")
                row["old"] = int(old) if isinstance(old, (int, float)) and not isinstance(old, bool) and old > 0 else None
            if "lp" in hs:
                lp = hs.get("lp")
                row["lp"] = max(0, int(lp)) if isinstance(lp, (int, float)) and not isinstance(lp, bool) else 0
            tm = str(hs.get("tm") or "")
            if tm in MODES and tm:
                row["tm"] = tm
            if "cs" in hs:
                cs = hs.get("cs")
                row["cs"] = max(0, int(cs)) if isinstance(cs, (int, float)) and not isinstance(cs, bool) else None
            self._dirty = True
            self._maybe_save(now_s)
            return dict(row)

    def rows(self, now=None):
        """Return a list of row dicts sorted by name/dev."""
        _ = now
        with self._lock:
            out = [dict(r, q=list(r["q"]) if isinstance(r.get("q"), list) else r.get("q"))
                   for r in self._rows.values()]
        out.sort(key=lambda r: ((r.get("name") or "~").lower(), r["dev"]))
        return out

    def _maybe_save(self, now_s):
        if self._dirty and (self._last_save == 0.0 or (now_s - self._last_save) >= SAVE_INTERVAL):
            self._write_locked(now_s)

    def _write_locked(self, now_s=None):
        if not self.path:
            return
        os.makedirs(self.folder, exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        doc = {"v": 1, "rows": list(self._rows.values())}
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self._dirty = False
            self._last_save = float(time.time() if now_s is None else now_s)
        except OSError:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def save(self, force=True):
        """Atomically write `team_seen.json` (throttled to at most every 30 s unless force=True)."""
        now_s = time.time()
        with self._lock:
            if not self._dirty and not (force and not os.path.isfile(self.path)):
                if not force or not self._dirty:
                    return
            if not force and self._last_save > 0.0 and (now_s - self._last_save) < SAVE_INTERVAL:
                return
            self._write_locked(now_s)
