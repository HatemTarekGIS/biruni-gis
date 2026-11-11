"""«فيلم المشروع»: the history of a map's features, day by day, for the page to play.

frames(svc, session, widget id) -> {"t": "show_film", "widget", "frames": [{"day", "t", "ch": [[lid, fid, colour]]}],
"start", "end", "count"} or {"t": "show_err", "why": …}

Where the history comes from:
1. the team room (team.current()): every accepted change of the map's layers, in order. A change's colour is the
   map's colour for the feature's state after it (a "categories" style reads the state's value of the style field;
   other styles give the feature's colour of today).
2. else a date field named in the map's ``options.film_date`` (e.g. date_done): each feature takes today's colour
   on its date.
Only the features the viewer's card may see (they are in the map data computed for that card) are ever sent, and
a film field the card hides is refused.
"""
import datetime
import time

from qgis.core import QgsFeatureRequest

MAX_DAYS = 400
MAX_CHANGES = 20000


def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _ts_of(v):
    """A date-ish attribute value -> epoch seconds or None."""
    if v is None:
        return None
    try:
        if hasattr(v, "toSecsSinceEpoch") and hasattr(v, "isValid"):       # QDateTime
            return float(v.toSecsSinceEpoch()) if v.isValid() else None
        if hasattr(v, "startOfDay") and hasattr(v, "isValid"):             # QDate
            return float(v.startOfDay().toSecsSinceEpoch()) + 12 * 3600 if v.isValid() else None
        if isinstance(v, (int, float)):
            return float(v) if v > 1e8 else None
        if isinstance(v, datetime.datetime):
            return v.timestamp()
        if isinstance(v, datetime.date):
            return time.mktime(v.timetuple()) + 12 * 3600
        s = str(v).strip()[:19]
        for text, fmt, noon in ((s, "%Y-%m-%dT%H:%M:%S", 0), (s, "%Y-%m-%d %H:%M:%S", 0),
                                (s[:10], "%Y-%m-%d", 12), (s[:10], "%Y/%m/%d", 12), (s[:10], "%d/%m/%Y", 12)):
            try:
                return time.mktime(time.strptime(text, fmt)) + noon * 3600
            except ValueError:
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def frames_from_changes(changes):
    """[(ts, lid, fid, colour)] -> the frames (one per day, the last colour of a feature that day)."""
    by_day = {}
    for ts, lid, fid, col in sorted(changes, key=lambda c: c[0]):
        by_day.setdefault(_day(ts), {"t": ts, "ch": {}})["ch"][(lid, fid)] = col
        by_day[_day(ts)]["t"] = ts
    days = sorted(by_day)[-MAX_DAYS:]
    out, n = [], 0
    for d in days:
        ch = [[lid, fid, col] for (lid, fid), col in by_day[d]["ch"].items()]
        n += len(ch)
        if n > MAX_CHANGES:
            break
        out.append({"day": d, "t": round(by_day[d]["t"]), "ch": ch})
    return out


def build(svc, s, wid):
    from . import mapdata

    d = svc._def_of(s) or {}
    w = next((x for x in d.get("widgets") or [] if x.get("id") == wid), None)
    if w is None or w.get("type") != "map":
        return {"t": "show_err", "why": "no film"}
    if s.card is not None:
        from . import model

        if wid not in model.allowed(d, s.card):
            return {"t": "show_err", "why": "no film"}
    hide = set((s.card or {}).get("hide") or ())
    res, _eff = svc._result(s)
    md = (res.get("widgets") or {}).get(wid) or {}
    layers = md.get("layers") if isinstance(md.get("layers"), list) else None
    entries = mapdata.layers_of(d, w) or []
    if not layers or not entries:
        return {"t": "show_err", "why": "no film"}
    comp = svc._computer(s)
    # what the card sees: {lid: {fid: today's colour}} and the colour of each category
    seen, cats = {}, {}
    for L in layers:
        if L.get("hidden"):
            continue
        lid = L.get("id")
        seen[lid] = {str(f.get("id")): (f.get("properties") or {}).get("_c")
                     for f in ((L.get("geojson") or {}).get("features") or [])}
        cats[lid] = {str(it.get("key")): it.get("color") for it in L.get("legend") or [] if it.get("key") is not None}
    changes = []
    node = None
    try:
        from .. import team

        node = team.current()
    except Exception:  # noqa: BLE001
        node = None
    if node is not None and getattr(node, "store", None) is not None:
        for n, entry in enumerate(entries[:8]):
            lid = f"L{n}"
            if lid not in seen:
                continue
            try:
                key = node.key_of(comp.layer(entry["source"]))
            except Exception:  # noqa: BLE001
                continue
            field = ((entry.get("style") or {}).get("field") if (entry.get("style") or {}).get("kind") == "categories"
                     else None)
            if field in hide:
                field = None
            rows = node.store._rows("SELECT seq, uid, epoch, author, name, ts, layer, fid, kind, state, prev, ver, "
                                    "sig, note, src FROM ops WHERE layer=? ORDER BY seq", (key,))
            for op in rows:
                fid = str(op.get("fid"))
                if fid not in seen[lid] or op.get("kind") == "field":
                    continue
                col = seen[lid][fid]
                st = op.get("state") or {}
                if field and isinstance(st.get("a"), dict) and field in st["a"]:
                    col = cats[lid].get(str(st["a"][field]), col)
                if op.get("state") is None:
                    col = None
                changes.append((float(op.get("ts") or 0), lid, fid, col))
    if not changes:
        fld = (w.get("options") or {}).get("film_date")
        if not fld:
            return {"t": "show_err", "why": "no history"}
        if fld in hide:
            return {"t": "show_err", "why": "no film"}
        for n, entry in enumerate(entries[:8]):
            lid = f"L{n}"
            if lid not in seen or not seen[lid]:
                continue
            lyr = comp.layer(entry["source"])
            idx = lyr.fields().indexOf(fld)
            if idx < 0:
                continue
            ids = []
            for f in seen[lid]:
                try:
                    ids.append(int(f))
                except ValueError:
                    pass
            req = QgsFeatureRequest().setFilterFids(ids).setNoAttributes()
            req.setSubsetOfAttributes([idx])
            req.setFlags(QgsFeatureRequest.Flag.NoGeometry)
            for feat in lyr.getFeatures(req):
                ts = _ts_of(feat[idx])
                fid = str(feat.id())
                if ts is not None and fid in seen[lid]:
                    changes.append((ts, lid, fid, seen[lid][fid]))
    if not changes:
        return {"t": "show_err", "why": "no history"}
    fr = frames_from_changes(changes)
    return {"t": "show_film", "widget": wid, "frames": fr, "start": fr[0]["t"] if fr else None,
            "end": fr[-1]["t"] if fr else None, "count": sum(len(f["ch"]) for f in fr)}
