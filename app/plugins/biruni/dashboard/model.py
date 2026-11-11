"""What a dashboard is: its definition (SPEC.md §1), checked, kept with the project, and cut down for a card.

Definitions live in the project's GeoPackage, table ``biruni_dashboards`` (an attributes table registered in
gpkg_contents), or - for a project without a GeoPackage - in the project file's own entries.
"""
import copy
import json
import os
import secrets
import sqlite3
import time

from qgis.core import QgsExpression, QgsProject

TABLE = "biruni_dashboards"
CARDS = "biruni_dashboard_cards"
REVOKED = "biruni_dashboard_revoked"
SCOPE = "biruni"
ENTRY = "dashboards"

TYPES = ("kpi", "gauge", "sparkline", "line", "area", "bar", "pie", "table", "map", "choropleth", "progress",
         "leaderboard", "feed", "text", "filter")
OPS = ("count", "sum", "avg", "min", "max", "distinct", "ratio")
TIMES = ("day", "week", "month", "year")
NEEDS_SOURCE = set(TYPES) - {"feed", "text", "filter"}
NEEDS_GROUP = {"sparkline", "line", "area", "bar", "pie", "progress", "leaderboard", "choropleth"}
THEMES = ("biruni-light", "biruni-dark")
MAP_STYLES = ("single", "categories", "graduated", "layer")
# what a viewer may see of a widget: never its source, measure, grouping or expressions
SHOWN = ("id", "type", "title", "x", "y", "w", "h", "style")
SAFE_OPTIONS = ("decimals", "unit", "legend", "stack", "labels", "columns", "zoom", "basemap", "orientation",
                "filter", "fields", "limit", "x_label", "y_label", "text")


def new_id(prefix="d"):
    return f"{prefix}_{secrets.token_hex(4)}"


def blank(title="Dashboard"):
    return {"v": 1, "id": new_id(), "title": title, "theme": THEMES[0], "columns": 12, "dates": "both",
            "k_min": 0, "sources": {}, "filters": [], "widgets": []}


def _expr_problem(text):
    if not text:
        return ""
    e = QgsExpression(text)
    return e.parserErrorString() if e.hasParserError() else ""


def _measure_problems(m, where):
    out = []
    if not isinstance(m, dict) or m.get("op") not in OPS:
        return [f"{where}: measure.op must be one of {', '.join(OPS)}"]
    if m["op"] not in ("count", "ratio") and not m.get("field"):
        out.append(f"{where}: measure {m['op']} needs a field")
    if m["op"] == "ratio":
        d = m.get("denominator") or {}
        if not isinstance(d, dict) or d.get("op") not in OPS or d.get("op") == "ratio":
            out.append(f"{where}: a ratio needs a denominator measure")
    p = _expr_problem(m.get("where"))
    if p:
        out.append(f"{where}: where: {p}")
    return out


def validate(d):
    """The problems of a definition (empty list = fine)."""
    out = []
    if not isinstance(d, dict):
        return ["not a dashboard"]
    if not d.get("id"):
        out.append("no id")
    if d.get("dates", "both") not in ("greg", "hijri", "both"):
        out.append("dates must be greg, hijri or both")
    try:
        if int(d.get("k_min") or 0) < 0:
            out.append("k_min must be 0 or more")
    except (TypeError, ValueError):
        out.append("k_min must be a number")
    sources = d.get("sources") or {}
    if not isinstance(sources, dict):
        return out + ["sources must be an object"]
    for sid, s in sources.items():
        if not isinstance(s, dict) or not s.get("layer"):
            out.append(f"source {sid}: no layer")
            continue
        p = _expr_problem(s.get("filter"))
        if p:
            out.append(f"source {sid}: filter: {p}")
    fids = set()
    for f in d.get("filters") or []:
        if not isinstance(f, dict) or not f.get("id") or f.get("source") not in sources or not f.get("field"):
            out.append(f"filter {f.get('id') if isinstance(f, dict) else f}: needs id, source and field")
            continue
        fids.add(f["id"])
    seen = set()
    for w in d.get("widgets") or []:
        wid = w.get("id") if isinstance(w, dict) else None
        where = f"widget {wid}"
        if not wid or wid in seen:
            out.append(f"{where}: missing or repeated id")
            continue
        seen.add(wid)
        t = w.get("type")
        if t not in TYPES:
            out.append(f"{where}: unknown type {t}")
            continue
        layers = (w.get("options") or {}).get("layers") if t == "map" else None
        if layers:
            if not isinstance(layers, list):
                out.append(f"{where}: options.layers must be a list")
            else:
                for n, L in enumerate(layers):
                    if not isinstance(L, dict) or L.get("source") not in sources:
                        out.append(f"{where}: layer {n}: unknown source")
                        continue
                    p = _expr_problem(L.get("where"))
                    if p:
                        out.append(f"{where}: layer {n}: where: {p}")
                    st = L.get("style") or {}
                    if st.get("kind", "single") not in MAP_STYLES:
                        out.append(f"{where}: layer {n}: style.kind must be one of {', '.join(MAP_STYLES)}")
                    elif st.get("kind") in ("categories", "graduated") and not st.get("field"):
                        out.append(f"{where}: layer {n}: a {st['kind']} style needs a field")
            if (w.get("options") or {}).get("basemap", "none") not in ("none", "project"):
                out.append(f"{where}: options.basemap must be none or project")
        elif t in NEEDS_SOURCE:
            if w.get("source") not in sources:
                out.append(f"{where}: unknown source")
            if t not in ("map",) and t != "table":
                out += _measure_problems(w.get("measure") or {"op": "count"}, where)
        g = w.get("group")
        if t in NEEDS_GROUP and not g:
            out.append(f"{where}: {t} needs a group")
        if g:
            if not isinstance(g, dict) or not (g.get("field") or g.get("zones")):
                out.append(f"{where}: group needs a field or zones")
            elif g.get("time") and g["time"] not in TIMES:
                out.append(f"{where}: group.time must be one of {', '.join(TIMES)}")
        if t == "choropleth" and not (g or {}).get("zones"):
            out.append(f"{where}: a choropleth groups by zones")
        if t == "progress":
            p = _expr_problem((w.get("options") or {}).get("done"))
            if p or not (w.get("options") or {}).get("done"):
                out.append(f"{where}: progress needs options.done (an expression) {p}".strip())
        if t == "filter" and (w.get("options") or {}).get("filter") not in fids:
            out.append(f"{where}: options.filter must name a filter")
        for k in ("x", "y", "w", "h"):
            try:
                if int(w.get(k, 0)) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                out.append(f"{where}: {k} must be a whole number")
    return out


# ---- what a viewer may see ---------------------------------------------------------------------------------------
def _cols(text):
    if not text:
        return set()
    e = QgsExpression(text)
    if e.hasParserError():
        return set()
    return {("*" if c.startswith("#!") else c) for c in e.referencedColumns() if c}


def widget_fields(d, w):
    """Every field a widget's numbers depend on (its measure, grouping, where, progress, source filter)."""
    f = set()
    m = w.get("measure") or {}
    for mm in (m, m.get("denominator") or {}):
        if mm.get("field"):
            f.add(mm["field"])
        f |= _cols(mm.get("where"))
    g = w.get("group") or {}
    if g.get("field"):
        f.add(g["field"])
    o = w.get("options") or {}
    f |= _cols(o.get("done"))
    if o.get("split"):
        f.add(o["split"])
    src = (d.get("sources") or {}).get(w.get("source")) or {}
    f |= _cols(src.get("filter"))
    for L in (o.get("layers") or []) if w.get("type") == "map" and isinstance(o.get("layers"), list) else []:
        if isinstance(L, dict):   # style, label and popup fields that are hidden are dropped, not the map
            f |= _cols(L.get("where"))
            f |= _cols(((d.get("sources") or {}).get(L.get("source")) or {}).get("filter"))
    if w.get("type") == "filter":
        flt = next((x for x in d.get("filters") or [] if x.get("id") == o.get("filter")), None)
        if flt:
            f.add(flt["field"])
    return f


def allowed(d, card):
    """The widget ids a card may see: its list (or all), less those whose numbers depend on a hidden field."""
    hide = set((card or {}).get("hide") or ())
    want = (card or {}).get("widgets", "*")
    out = []
    for w in d.get("widgets") or []:
        if want != "*" and w.get("id") not in (want or ()):
            continue
        used = widget_fields(d, w)
        if hide and (used & hide or "*" in used):
            continue
        out.append(w["id"])
    return out


def for_card(d, card):
    """The definition as a viewer gets it: only the card's widgets, no sources, expressions or layer ids."""
    hide = set((card or {}).get("hide") or ())
    ids = set(allowed(d, card))
    forced = (card or {}).get("filters") or {}
    widgets = []
    for w in d.get("widgets") or []:
        if w["id"] not in ids:
            continue
        c = {k: copy.deepcopy(w[k]) for k in SHOWN if k in w}
        style = c.get("style") or {}
        if style.get("by") in hide:
            style.pop("by", None)
        opts = {k: copy.deepcopy(v) for k, v in (w.get("options") or {}).items() if k in SAFE_OPTIONS}
        if isinstance(opts.get("fields"), list):
            opts["fields"] = [x for x in opts["fields"] if x not in hide]
        if opts:
            c["options"] = opts
        g = w.get("group") or {}
        if g.get("time"):
            c["time"] = g["time"]
        widgets.append(c)
    filters = [{"id": f["id"], "label": f.get("label") or f["id"], "locked": f["id"] in forced}
               for f in d.get("filters") or [] if f.get("field") not in hide]
    return {"v": 1, "id": d.get("id"), "title": d.get("title", ""), "theme": d.get("theme", THEMES[0]),
            "columns": int(d.get("columns") or 12), "dates": d.get("dates", "both"), "filters": filters,
            "widgets": widgets}


# ---- keeping definitions -----------------------------------------------------------------------------------------
def _gpkg(project):
    try:
        from ..project import project_gpkg

        p = project_gpkg(project)
    except Exception:  # noqa: BLE001
        p = None
    return p if p and os.path.exists(p) else None


def _con(gpkg):
    con = sqlite3.connect(gpkg, timeout=20)
    con.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (id TEXT PRIMARY KEY, title TEXT, def TEXT, updated REAL)")
    con.execute(f"CREATE TABLE IF NOT EXISTS {CARDS} (id TEXT PRIMARY KEY, dash TEXT, name TEXT, iat REAL, "
                "exp REAL, payload TEXT)")
    con.execute(f"CREATE TABLE IF NOT EXISTS {REVOKED} (id TEXT PRIMARY KEY, dash TEXT, ts REAL)")
    try:
        for t in (TABLE, CARDS, REVOKED):
            con.execute("INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, last_change) "
                        "VALUES (?, 'attributes', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))", (t, t))
    except sqlite3.Error:
        pass  # not a GeoPackage made by GDAL: the tables still work
    con.commit()
    return con


def _entries(project, name):
    raw, ok = project.readEntry(SCOPE, name, "")
    try:
        v = json.loads(raw) if ok and raw else {}
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _put_entries(project, name, value):
    project.writeEntry(SCOPE, name, json.dumps(value, ensure_ascii=False))
    project.setDirty(True)


def load_all(project=None):
    """The dashboards of the project, in the order they were saved."""
    project = project or QgsProject.instance()
    gpkg = _gpkg(project)
    if gpkg:
        con = _con(gpkg)
        try:
            rows = con.execute(f"SELECT def FROM {TABLE} ORDER BY updated").fetchall()
        finally:
            con.close()
        out = []
        for (raw,) in rows:
            try:
                out.append(json.loads(raw))
            except ValueError:
                pass
        return out
    return list(_entries(project, ENTRY).values())


def get(project, dash_id):
    return next((d for d in load_all(project) if d.get("id") == dash_id), None)


def save(project, definition):
    project = project or QgsProject.instance()
    d = copy.deepcopy(definition)
    d.setdefault("v", 1)
    if not d.get("id"):
        d["id"] = new_id()
    gpkg = _gpkg(project)
    if gpkg:
        con = _con(gpkg)
        try:
            con.execute(f"INSERT OR REPLACE INTO {TABLE} (id, title, def, updated) VALUES (?, ?, ?, ?)",
                        (d["id"], d.get("title", ""), json.dumps(d, ensure_ascii=False), time.time()))
            con.commit()
        finally:
            con.close()
    else:
        e = _entries(project, ENTRY)
        e[d["id"]] = d
        _put_entries(project, ENTRY, e)
    return d


def delete(project, dash_id):
    project = project or QgsProject.instance()
    gpkg = _gpkg(project)
    if gpkg:
        con = _con(gpkg)
        try:
            con.execute(f"DELETE FROM {TABLE} WHERE id=?", (dash_id,))
            con.commit()
        finally:
            con.close()
    else:
        e = _entries(project, ENTRY)
        e.pop(dash_id, None)
        _put_entries(project, ENTRY, e)


# ---- issued and revoked cards (kept where the definitions are) -------------------------------------------------
def keep_card(project, payload):
    project = project or QgsProject.instance()
    gpkg = _gpkg(project)
    if gpkg:
        con = _con(gpkg)
        try:
            con.execute(f"INSERT OR REPLACE INTO {CARDS} (id, dash, name, iat, exp, payload) VALUES (?,?,?,?,?,?)",
                        (payload["id"], payload["dash"], payload.get("name", ""), payload.get("iat"),
                         payload.get("exp"), json.dumps(payload, ensure_ascii=False)))
            con.commit()
        finally:
            con.close()
    else:
        e = _entries(project, "dashboard_cards")
        e[payload["id"]] = payload
        _put_entries(project, "dashboard_cards", e)


def issued_cards(project=None, dash_id=None):
    project = project or QgsProject.instance()
    gpkg = _gpkg(project)
    if gpkg:
        con = _con(gpkg)
        try:
            rows = [json.loads(r[0]) for r in con.execute(f"SELECT payload FROM {CARDS} ORDER BY iat")]
        finally:
            con.close()
    else:
        rows = list(_entries(project, "dashboard_cards").values())
    return [r for r in rows if dash_id is None or r.get("dash") == dash_id]


def revoke_card(project, card_id, dash_id=""):
    project = project or QgsProject.instance()
    gpkg = _gpkg(project)
    if gpkg:
        con = _con(gpkg)
        try:
            con.execute(f"INSERT OR REPLACE INTO {REVOKED} (id, dash, ts) VALUES (?, ?, ?)",
                        (card_id, dash_id, time.time()))
            con.commit()
        finally:
            con.close()
    else:
        e = _entries(project, "dashboard_revoked")
        e[card_id] = {"dash": dash_id, "ts": time.time()}
        _put_entries(project, "dashboard_revoked", e)


def revoked_ids(project=None):
    project = project or QgsProject.instance()
    gpkg = _gpkg(project)
    if gpkg:
        con = _con(gpkg)
        try:
            return {r[0] for r in con.execute(f"SELECT id FROM {REVOKED}")}
        finally:
            con.close()
    return set(_entries(project, "dashboard_revoked"))
