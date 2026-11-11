"""Shared parts of the "show" extension (ext_show.py): the per-widget history the narrative, the forecast and the film
read, and the viewers' last visits.

History: after every push to a viewer the service has the numbers that viewer sees (cut to its card's zone, k_min
and hidden fields already applied). showkit keeps, per dashboard and card, one summary a day (the last of the day)
of the numbers: kpi/gauge values, bar/pie/leaderboard items, progress items. It observes the service by wrapping
DashboardService._push (no change to service.py); the summary is small (numbers only, never a geometry or a
text value beyond the item labels the viewer already sees).

Everything is kept in the settings folder (biruni/dashboard_history.json, biruni/dashboard_visits.json), written
at most once a minute.
"""
import json
import os
import threading
import time

KEEP_DAYS = 400
WRITE_EVERY = 60.0

_lock = threading.RLock()
_hist = None          # {"<dash>|<card>": {"YYYY-MM-DD": {"ts": t, "w": summary}}}
_visits = None        # {"<dash>|<card>": {"ts": t, "w": summary}}
_dirty = {"h": 0.0, "v": 0.0}
_folder_override = []


def folder():
    if _folder_override:
        return _folder_override[0]
    try:
        from qgis.core import QgsApplication

        base = QgsApplication.qgisSettingsDirPath()
    except Exception:  # noqa: BLE001
        base = os.path.expanduser("~")
    f = os.path.join(base, "biruni")
    os.makedirs(f, exist_ok=True)
    return f


def set_folder(path):
    """Tests: keep the files somewhere else (None = the settings folder)."""
    global _hist, _visits
    with _lock:
        _folder_override[:] = [path] if path else []
        _hist = None
        _visits = None


def _load(name):
    try:
        with open(os.path.join(folder(), name), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(name, d):
    p = os.path.join(folder(), name)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)
    except OSError:
        pass


def key_of(dash, card):
    return f"{dash}|{(card or {}).get('id') or 'preview'}"


# ---- the summary of what a viewer sees -----------------------------------------------------------------------------
def summary(widgets, def_widgets):
    """{wid: {"type", "v"?: number, "items"?: {key: value}, "prog"?: {key: [done, total]}, "labels"?: {key: label}}}"""
    types = {w.get("id"): w.get("type") for w in def_widgets or []}
    out = {}
    for wid, d in (widgets or {}).items():
        t = types.get(wid)
        if not isinstance(d, dict) or d.get("hidden") or "error" in d:
            continue
        if t in ("kpi", "gauge"):
            v = d.get("value")
            if isinstance(v, (int, float)):
                out[wid] = {"type": t, "v": v}
        elif t in ("bar", "pie", "leaderboard"):
            items, labels = {}, {}
            for it in d.get("items") or []:
                if not isinstance(it, dict) or it.get("hidden") or not isinstance(it.get("value"), (int, float)):
                    continue
                k = str(it.get("key", it.get("label", "")))
                items[k] = it["value"]
                labels[k] = str(it.get("label", k))[:80]
            if items:
                out[wid] = {"type": t, "items": items, "labels": labels}
        elif t == "progress":
            prog, labels = {}, {}
            for it in d.get("items") or []:
                if not isinstance(it, dict) or it.get("hidden"):
                    continue
                done, total = it.get("done"), it.get("total")
                if not isinstance(done, (int, float)) or not isinstance(total, (int, float)):
                    continue
                k = str(it.get("key", it.get("label", "")))
                prog[k] = [done, total]
                labels[k] = str(it.get("label", k))[:80]
            if prog:
                out[wid] = {"type": t, "prog": prog, "labels": labels, "xf": d.get("xf")}
    return out


# ---- history --------------------------------------------------------------------------------------------------------
def _h():
    global _hist
    if _hist is None:
        _hist = _load("dashboard_history.json")
    return _hist


def record(dash, card, summ, now=None):
    """Keep today's summary (the last of the day) for this dashboard and card."""
    if not summ:
        return
    now = now or time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    with _lock:
        h = _h().setdefault(key_of(dash, card), {})
        h[day] = {"ts": now, "w": summ}
        if len(h) > KEEP_DAYS:
            for d in sorted(h)[:len(h) - KEEP_DAYS]:
                del h[d]
        if time.time() - _dirty.get("hw", 0) > WRITE_EVERY:
            flush()


def history(dash, card):
    """[(day, ts, summary)] oldest first."""
    with _lock:
        h = _h().get(key_of(dash, card)) or {}
        return [(d, h[d]["ts"], h[d]["w"]) for d in sorted(h)]


def put_history(dash, card, days):
    """Tests and the showcase: set the history ({day: summary}) at once."""
    with _lock:
        h = _h().setdefault(key_of(dash, card), {})
        for day, summ in days.items():
            ts = time.mktime(time.strptime(day, "%Y-%m-%d")) + 18 * 3600
            h[day] = {"ts": ts, "w": summ}


def flush():
    with _lock:
        if _hist is not None:
            _save("dashboard_history.json", _hist)
        if _visits is not None:
            _save("dashboard_visits.json", _visits)
        _dirty["hw"] = time.time()


# ---- visits ---------------------------------------------------------------------------------------------------------
def _v():
    global _visits
    if _visits is None:
        _visits = _load("dashboard_visits.json")
    return _visits


def last_visit(dash, card):
    with _lock:
        return _v().get(key_of(dash, card))


def visit(dash, card, summ, now=None):
    with _lock:
        _v()[key_of(dash, card)] = {"ts": now or time.time(), "w": summ}
        if time.time() - _dirty.get("hw", 0) > WRITE_EVERY:
            flush()


# ---- observing the service ------------------------------------------------------------------------------------------
_RECORD_EVERY = 30.0
_last_rec = {}


def observe():
    """Wrap DashboardService._push once so that every viewer's numbers feed the history (at most every 30 s per
    dashboard and card)."""
    from . import service

    cls = service.DashboardService
    if getattr(cls._push, "_showkit", False):
        return
    orig = cls._push

    def _push(self, s):
        orig(self, s)
        try:
            k = key_of(s.dash_id, s.card)
            now = time.time()
            if now - _last_rec.get(k, 0) < _RECORD_EVERY:
                return
            _last_rec[k] = now
            d = self._def_of(s)
            if d is None:
                return
            res, _eff = self._result(s)
            record(s.dash_id, s.card, summary(res.get("widgets"), d.get("widgets")), now)
        except Exception:  # noqa: BLE001 - history never stops a push
            pass

    _push._showkit = True
    cls._push = _push


def current(svc, s):
    """(summary of what session s sees now, its definition for the card, the raw widget data)."""
    from . import model

    d = svc._def_of(s)
    if d is None:
        return {}, {"widgets": []}, {}
    res, _eff = svc._result(s)
    return summary(res.get("widgets"), d.get("widgets")), model.for_card(d, s.card), res.get("widgets") or {}
