"""The numbers of a dashboard (SPEC.md §1-2): every widget's data shape, computed on this PC from QGIS layers.

Speed: a source's rows (the attribute values the dashboard needs, no geometry) are read once into columns and
kept; an edit reloads only the features it touched. Expressions (a source's filter, a widget's ``where``, a
progress's ``done``) run in QGIS (a filtered request) and are kept as sets of feature ids; the card's zone is
a set of ids too (the features that intersect it). Geometry is read only for maps, zones and the zone cut.

Privacy: the card's zone cuts every source before any number is made; hidden fields are never read into
anything sent; groups with fewer than k_min features are withheld ({"hidden": true}).
"""
import datetime
import hashlib
import json
import math

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsCsException, QgsExpression,
                       QgsFeatureRequest, QgsGeometry, QgsProject, QgsRectangle, QgsSpatialIndex, QgsWkbTypes)
from qgis.PyQt.QtCore import QDate, QDateTime, QTime

from . import model

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
MAX_MAP = 5000
MAX_ROWS = 200
MAX_EXPLAIN = 50
MAX_SERIES = 5
MAX_OPTIONS = 200
NO_GEOM = Qgis.FeatureRequestFlag.NoGeometry
POINT = Qgis.GeometryType.Point


class DataError(Exception):
    pass


def is_null(v):
    if v is None:
        return True
    try:
        return v.isNull()  # QVariant NULL, an invalid QDate
    except AttributeError:
        return False


def epoch(v):
    """Seconds since 1970 of a date / time / number / ISO text value, or None."""
    if is_null(v):
        return None
    if isinstance(v, QDateTime):
        return float(v.toSecsSinceEpoch()) if v.isValid() else None
    if isinstance(v, QDate):
        return float(QDateTime(v, QTime(0, 0)).toSecsSinceEpoch()) if v.isValid() else None
    if isinstance(v, (datetime.datetime, datetime.date)):
        if not isinstance(v, datetime.datetime):
            v = datetime.datetime(v.year, v.month, v.day)
        return v.timestamp()
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if abs(v) > 1e11 else float(v)
    if isinstance(v, str) and v.strip():
        t = v.strip().replace("/", "-").replace("Z", "")
        try:
            return datetime.datetime.fromisoformat(t[:26]).timestamp()
        except ValueError:
            return None
    return None


def bucket(e, unit, week_start=5):
    """The start (epoch) of the day / week / month / year holding epoch e (local time). week_start: 0 = Monday …
    5 = Saturday (the default: the week of most Arab countries)."""
    d = datetime.datetime.fromtimestamp(e)
    d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "week":
        d -= datetime.timedelta(days=(d.weekday() - week_start) % 7)
    elif unit == "month":
        d = d.replace(day=1)
    elif unit == "year":
        d = d.replace(month=1, day=1)
    return d.timestamp()


def number(v):
    if is_null(v) or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    try:
        f = float(str(v).replace(",", ""))
        return f if math.isfinite(f) else None
    except ValueError:
        return None


def clean(v):
    """A value as JSON can carry it."""
    if is_null(v):
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        return int(v) if v.is_integer() and abs(v) < 1e15 else round(v, 6)
    if isinstance(v, QDateTime):
        return v.toString("yyyy-MM-ddTHH:mm:ss") if v.isValid() else None
    if isinstance(v, QDate):
        return v.toString("yyyy-MM-dd") if v.isValid() else None
    if isinstance(v, QTime):
        return v.toString("HH:mm:ss") if v.isValid() else None
    if isinstance(v, (bytes, bytearray)):
        return None
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    return str(v)[:500]


def key_of(v):
    if is_null(v):
        return ""
    c = clean(v)
    return c if isinstance(c, str) else json.dumps(c)


def _round(v):
    return None if v is None else clean(float(v))


def _and(a, b):
    a, b = (a or "").strip(), (b or "").strip()
    if a and b:
        return f"({a}) AND ({b})"
    return a or b


M256 = 1 << 256


class Rows:
    """One source's rows: feature ids and the needed fields' values as columns, updated in place after an edit.

    Derived columns (a field's group keys, numbers, time buckets) are made once and kept up to date row by row.
    The digest of the rows (what a seal says the number was computed from) is the sum mod 2^256 of every row's
    sha256 (fid and values): the same rows give the same digest in any order, and an edit updates it at once."""

    def __init__(self, fids, cols):
        self.fids = fids
        self.cols = cols
        self.names = sorted(cols)
        self.index = {f: i for i, f in enumerate(fids)}
        self.derived = {}     # key -> [field, fn, list]
        self._h = None        # per-row hashes
        self._sum = 0

    def __len__(self):
        return len(self.fids)

    def derive(self, key, field, fn):
        d = self.derived.get(key)
        if d is None:
            col = self.cols.get(field)
            lst = [fn(v) for v in col] if col is not None else [None] * len(self.fids)
            d = self.derived[key] = [field, fn, lst]
        return d[2]

    def _rowhash(self, i):
        text = json.dumps([self.fids[i]] + [clean(self.cols[n][i]) for n in self.names], ensure_ascii=False,
                          default=str)
        return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest(), "big")

    def digest(self):
        if self._h is None:
            self._h = [self._rowhash(i) for i in range(len(self.fids))]
            self._sum = sum(self._h) % M256
        head = hashlib.sha256(json.dumps(self.names).encode()).hexdigest()[:16]
        return f"{head}:{self._sum:064x}"

    def put(self, fid, values):
        """Set (or add) one feature's values {field: value}."""
        i = self.index.get(fid)
        if i is None:
            i = len(self.fids)
            self.fids.append(fid)
            self.index[fid] = i
            for n in self.names:
                self.cols[n].append(values.get(n))
            for field, fn, lst in self.derived.values():
                lst.append(fn(values.get(field)) if field in self.cols else None)
            if self._h is not None:
                h = self._rowhash(i)
                self._h.append(h)
                self._sum = (self._sum + h) % M256
            return
        for n in self.names:
            self.cols[n][i] = values.get(n)
        for field, fn, lst in self.derived.values():
            lst[i] = fn(values.get(field)) if field in self.cols else None
        if self._h is not None:
            h = self._rowhash(i)
            self._sum = (self._sum - self._h[i] + h) % M256
            self._h[i] = h

    def remove(self, fid):
        i = self.index.pop(fid, None)
        if i is None:
            return
        last = len(self.fids) - 1
        lists = [self.fids] + [self.cols[n] for n in self.names] + [d[2] for d in self.derived.values()]
        if self._h is not None:
            self._sum = (self._sum - self._h[i]) % M256
            lists.append(self._h)
        if i != last:
            for lst in lists:
                lst[i] = lst[last]
            self.index[self.fids[i]] = i
        for lst in lists:
            lst.pop()


class Computer:
    """Computes a dashboard's widgets. Keep one per dashboard; call invalidate() when its layers change."""

    def __init__(self, definition, project=None):
        self.d = definition
        self.project = project or QgsProject.instance()
        self._rows = {}       # sid -> Rows
        self._masks = {}      # (sid, expr) -> set of fids
        self._zone = {}       # (sid, wkt) -> set of fids
        self._zones = {}      # (sid, zones layer id, label field) -> {fid: zone key}, {key: label}
        self._last = {}       # (widget, scope) -> (value, prev): kpi prev / delta
        self.edit_log = []    # [{"ts","who","what","layer","kind","fid","layer_id"}] when not in a team room
        self._sel = {}        # selections made during one compute()
        self._geo = None      # map layers' geometries (mapdata.Geometries)

    def geometries(self):
        if self._geo is None:
            from .mapdata import Geometries

            self._geo = Geometries(self.project)
        return self._geo

    # ---- the layers ------------------------------------------------------------------------------------------
    def source(self, sid):
        return (self.d.get("sources") or {}).get(sid) or {}

    def layer(self, sid):
        lid = self.source(sid).get("layer")
        lyr = self.project.mapLayer(lid) if lid else None
        if lyr is None or not hasattr(lyr, "getFeatures"):
            raise DataError(f"source {sid}: the layer is not in the project")
        return lyr

    def layer_ids(self):
        ids = {s.get("layer") for s in (self.d.get("sources") or {}).values()}
        for w in self.d.get("widgets") or []:
            z = (w.get("group") or {}).get("zones")
            if z:
                ids.add(z)
        return {i for i in ids if i}

    def _fields(self, sid):
        """The fields read for a source: what any widget or filter needs, never more."""
        need = set()
        for w in self.d.get("widgets") or []:
            if w.get("source") != sid:
                continue
            m = w.get("measure") or {}
            for mm in (m, m.get("denominator") or {}):
                if mm.get("field"):
                    need.add(mm["field"])
            g = w.get("group") or {}
            if g.get("field"):
                need.add(g["field"])
            o = w.get("options") or {}
            for k in ("split", "label"):
                if o.get(k):
                    need.add(o[k])
            if isinstance(o.get("fields"), list):
                need |= set(o["fields"])
            if (w.get("style") or {}).get("by"):
                need.add(w["style"]["by"])
        from .mapdata import fields_needed

        need |= fields_needed(self.d, sid)
        for f in self.d.get("filters") or []:
            if f.get("source") == sid:
                need.add(f["field"])
        return need

    def _load(self, sid):
        lyr = self.layer(sid)
        names = [n for n in sorted(self._fields(sid)) if lyr.fields().indexOf(n) >= 0]
        req = QgsFeatureRequest().setFlags(NO_GEOM)
        expr = self.source(sid).get("filter")
        if expr:
            req.setFilterExpression(expr)
        req.setSubsetOfAttributes(names, lyr.fields())
        idx = [lyr.fields().indexOf(n) for n in names]
        fids, cols = [], {n: [] for n in names}
        lists = [cols[n] for n in names]
        for f in lyr.getFeatures(req):
            fids.append(f.id())
            a = f.attributes()
            for col, i in zip(lists, idx):
                col.append(a[i])
        return Rows(fids, cols)

    def rows(self, sid):
        r = self._rows.get(sid)
        if r is None:
            r = self._rows[sid] = self._load(sid)
        return r

    def invalidate(self, layer_id=None, fids=None):
        """Something changed: all (layer_id None), one layer, or only some of its features (fids)."""
        sids = [s for s, v in (self.d.get("sources") or {}).items() if layer_id is None or v.get("layer") == layer_id]
        zone_layers = layer_id is not None and any(k[1] == layer_id for k in self._zones)
        if zone_layers:
            self._zones = {k: v for k, v in self._zones.items() if k[1] != layer_id}
        if self._geo is not None:
            self._geo.invalidate(layer_id, None if fids is None else set(fids))
        if fids is None or layer_id is None:
            for sid in sids:
                self._rows.pop(sid, None)
                self._masks = {k: v for k, v in self._masks.items() if k[0] != sid}
                self._zone = {k: v for k, v in self._zone.items() if k[0] != sid}
                self._zones = {k: v for k, v in self._zones.items() if k[0] != sid}
            return
        fids = set(fids)
        for sid in sids:
            self._update(sid, fids)

    def _update(self, sid, fids):
        """Reload some features of a source in its rows, masks and zone sets."""
        r = self._rows.get(sid)
        try:
            lyr = self.layer(sid)
        except DataError:
            self._rows.pop(sid, None)
            return
        if r is not None:
            names = list(r.cols)
            req = QgsFeatureRequest().setFilterFids(list(fids)).setFlags(NO_GEOM)
            expr = self.source(sid).get("filter")
            if expr:
                req.setFilterExpression(expr)
            req.setSubsetOfAttributes(names, lyr.fields())
            idx = [lyr.fields().indexOf(n) for n in names]
            got = {}
            for f in lyr.getFeatures(req):
                a = f.attributes()
                got[f.id()] = {n: (a[i] if i >= 0 else None) for n, i in zip(names, idx)}
            for fid in fids:
                if fid in got:
                    r.put(fid, got[fid])
                else:
                    r.remove(fid)      # deleted, or no longer passes the source's filter
        for k in [k for k in self._masks if k[0] == sid]:
            s = self._masks[k] - fids
            req = QgsFeatureRequest().setFilterFids(list(fids)).setFlags(NO_GEOM).setNoAttributes()
            req.setFilterExpression(_and(self.source(sid).get("filter"), k[1]))
            s |= {f.id() for f in lyr.getFeatures(req)}
            self._masks[k] = s
        for k in [k for k in self._zone if k[0] == sid]:
            s = self._zone[k] - fids
            s |= self._zone_fids(lyr, k[1], fids)
            self._zone[k] = s
        for k in [k for k in self._zones if k[0] == sid]:
            of, labels = self._zones[k]
            for f in fids:
                of.pop(f, None)
            of.update(self._zones_of(sid, k[1], k[2], fids)[0])

    # ---- sets of features --------------------------------------------------------------------------------------
    def mask(self, sid, expr):
        k = (sid, expr)
        s = self._masks.get(k)
        if s is None:
            lyr = self.layer(sid)
            req = QgsFeatureRequest().setFlags(NO_GEOM).setNoAttributes()
            req.setFilterExpression(_and(self.source(sid).get("filter"), expr))
            s = self._masks[k] = {f.id() for f in lyr.getFeatures(req)}
        return s

    def _zone_geom(self, lyr, wkt):
        g = QgsGeometry.fromWkt(wkt)
        if lyr.crs().isValid() and lyr.crs() != WGS84:
            try:
                g.transform(QgsCoordinateTransform(WGS84, lyr.crs(), self.project))
            except QgsCsException:
                return None
        return g

    def _zone_fids(self, lyr, wkt, only=None):
        g = self._zone_geom(lyr, wkt)
        if g is None or g.isEmpty():
            return set()
        eng = QgsGeometry.createGeometryEngine(g.constGet())
        eng.prepareGeometry()
        req = QgsFeatureRequest().setNoAttributes().setFilterRect(g.boundingBox())
        if only is not None:
            req.setFilterFids(list(only))
        out = set()
        for f in lyr.getFeatures(req):
            fg = f.geometry()
            # touching only along the edge is not inside (a neighbouring district is not the card's)
            if not fg.isEmpty() and eng.intersects(fg.constGet()) and (fg.type() == POINT or
                                                                        not eng.touches(fg.constGet())):
                out.add(f.id())
        return out

    def zone(self, sid, wkt):
        k = (sid, wkt)
        s = self._zone.get(k)
        if s is None:
            s = self._zone[k] = self._zone_fids(self.layer(sid), wkt)
        return s

    def _zones_of(self, sid, zid, label, only=None):
        """{fid: zone key} for the source's features (the zone holding each feature's inner point), {key: label}."""
        lyr = self.layer(sid)
        zl = self.project.mapLayer(zid)
        if zl is None:
            raise DataError("the zones layer is not in the project")
        tr = None
        if zl.crs() != lyr.crs():
            tr = QgsCoordinateTransform(zl.crs(), lyr.crs(), self.project)
        zones, labels, index = {}, {}, QgsSpatialIndex()
        li = zl.fields().indexOf(label) if label else -1
        for z in zl.getFeatures():
            g = QgsGeometry(z.geometry())
            if g.isEmpty():
                continue
            if tr is not None:
                try:
                    g.transform(tr)
                except QgsCsException:
                    continue
            key = key_of(z.attributes()[li]) if li >= 0 else str(z.id())
            eng = QgsGeometry.createGeometryEngine(g.constGet())
            eng.prepareGeometry()
            zones[z.id()] = (key, eng)
            labels[key] = key
            index.addFeature(z.id(), g.boundingBox())
        out = {}
        req = QgsFeatureRequest().setNoAttributes()
        if only is not None:
            req.setFilterFids(list(only))
        for f in lyr.getFeatures(req):
            g = f.geometry()
            if g.isEmpty():
                continue
            p = g if g.type() == POINT else g.pointOnSurface()
            r = p.boundingBox()
            for zid_ in index.intersects(r):
                key, eng = zones[zid_]
                if eng.intersects(p.constGet()):
                    out[f.id()] = key
                    break
        return out, labels

    def zones_of(self, sid, zid, label):
        k = (sid, zid, label or "")
        v = self._zones.get(k)
        if v is None:
            v = self._zones[k] = self._zones_of(sid, zid, label)
        return v

    # ---- what a card and the viewer's filters let through ------------------------------------------------------
    def effective_filters(self, card=None, filters=None):
        hide = set((card or {}).get("hide") or ())
        out = {}
        defs = {f["id"]: f for f in self.d.get("filters") or [] if f.get("id")}
        for fid, vals in (filters or {}).items():
            if fid in defs and vals is not None and defs[fid].get("field") not in hide:
                out[fid] = [str(v) for v in (vals if isinstance(vals, list) else [vals])][:MAX_OPTIONS]
        for fid, vals in ((card or {}).get("filters") or {}).items():
            if fid in defs:
                out[fid] = [str(v) for v in (vals if isinstance(vals, list) else [vals])]
        return out

    def selection(self, sid, card=None, filters=None, where=None, skip=None):
        """Indices into the source's rows that pass the card's zone, the filters (all but ``skip``) and ``where``."""
        r = self.rows(sid)
        sets = []
        if (card or {}).get("zone"):
            sets.append(self.zone(sid, card["zone"]))
        if where:
            sets.append(self.mask(sid, where))
        tests = []
        for f in self.d.get("filters") or []:
            if f.get("source") != sid or f["id"] == skip or f["id"] not in (filters or {}):
                continue
            if f["field"] in r.cols:
                tests.append((self.keys(r, f["field"]), set(filters[f["id"]])))
        ck = (sid, (card or {}).get("zone"), where, json.dumps(sorted((str(id(k)), sorted(v)) for k, v in tests)))
        hit = self._sel.get(ck)
        if hit is not None and hit[0] is r and hit[1] == len(r):
            return hit[2]
        if not sets and not tests:
            out = list(range(len(r.fids)))
        else:
            out = list(range(len(r.fids)))
            fids = r.fids
            for s in sets:
                out = [i for i in out if fids[i] in s]
            for keys, vals in tests:
                out = [i for i in out if keys[i] in vals]
        self._sel[ck] = (r, len(r), out)
        return out

    @staticmethod
    def keys(r, field):
        return r.derive(("key", field), field, key_of)

    @staticmethod
    def numbers(r, field):
        return r.derive(("num", field), field, number)

    # ---- measures ------------------------------------------------------------------------------------------------
    def measure(self, sid, m, idx):
        """(value, n features) of a measure over row indices."""
        m = m or {"op": "count"}
        op = m.get("op", "count")
        r = self.rows(sid)
        if op == "ratio":   # numerator: this measure's field and where with op "of"; denominator: its own
            num, n1 = self.measure(sid, {k: v for k, v in m.items() if k not in ("denominator", "op", "of")} |
                                   {"op": m.get("of", "count")}, idx)
            den, _ = self.measure(sid, m.get("denominator") or {"op": "count"}, idx)
            return (num / den if den and num is not None else None), n1
        if m.get("where"):
            s = self.mask(sid, m["where"])
            idx = [i for i in idx if r.fids[i] in s]
        n = len(idx)
        if op == "count":
            return float(n), n
        if m.get("field") not in r.cols:
            return None, n
        if op == "distinct":
            keys = self.keys(r, m["field"])
            return float(len({keys[i] for i in idx if keys[i] != ""})), n
        nums = self.numbers(r, m["field"])
        vals = [v for v in (nums[i] for i in idx) if v is not None]
        if not vals:
            return None, n
        if op == "sum":
            return math.fsum(vals), n
        if op == "avg":
            return math.fsum(vals) / len(vals), n
        if op == "min":
            return min(vals), n
        if op == "max":
            return max(vals), n
        return None, n

    def groups(self, sid, w, idx):
        """[(key, label, x, idx)] of a widget's grouping, ordered."""
        g = w.get("group") or {}
        r = self.rows(sid)
        buckets, labels, xs = {}, {}, {}
        if g.get("zones"):
            of, zl = self.zones_of(sid, g["zones"], g.get("label"))
            for i in idx:
                k = of.get(r.fids[i])
                if k is None:
                    continue
                buckets.setdefault(k, []).append(i)
            labels = zl
        elif g.get("time"):
            if g.get("field") not in r.cols:
                return []
            ws = int(g.get("week_start", 5))
            unit = g["time"]

            def tb(v, unit=unit, ws=ws):
                e = epoch(v)
                if e is None:
                    return None
                b = bucket(e, unit, ws)
                return datetime.datetime.fromtimestamp(b).strftime("%Y-%m-%d"), b
            tbs = r.derive(("time", g["field"], unit, ws), g["field"], tb)
            for i in idx:
                t = tbs[i]
                if t is None:
                    continue
                buckets.setdefault(t[0], []).append(i)
                xs[t[0]] = t[1]
        else:
            if g.get("field") not in r.cols:
                return []
            keys = self.keys(r, g["field"])
            col = r.cols[g["field"]]
            for i in idx:
                k = keys[i]
                b = buckets.get(k)
                if b is None:
                    b = buckets[k] = []
                    labels[k] = k if k != "" else "—"
                    n = number(col[i])
                    if n is not None and not isinstance(col[i], str):
                        xs[k] = n
                b.append(i)
        out = [(k, labels.get(k, k), xs.get(k), v) for k, v in buckets.items()]
        return out

    # ---- the widgets ---------------------------------------------------------------------------------------------
    def compute(self, card=None, filters=None, only=None):
        """{"widgets": {id: data}, "digests": {source id: digest of its rows}, "errors": {id: text}}."""
        k = int(self.d.get("k_min") or 0)
        eff = self.effective_filters(card, filters)
        ids = set(model.allowed(self.d, card)) if card is not None else {w["id"] for w in self.d.get("widgets") or []}
        if only is not None:
            ids &= set(only)
        hide = set((card or {}).get("hide") or ())
        out, digests, errors = {}, {}, {}
        self._sel = {}
        scope = json.dumps([(card or {}).get("id"), eff], sort_keys=True)
        for w in self.d.get("widgets") or []:
            if w.get("id") not in ids:
                continue
            try:
                out[w["id"]] = self.widget(w, card, eff, k, hide, scope)
                sid = w.get("source")
                sids = [sid]
                if w.get("type") == "map":
                    from .mapdata import layers_of

                    sids += [L["source"] for L in layers_of(self.d, w) or ()]
                for sid in sids:
                    if sid in (self.d.get("sources") or {}) and sid not in digests:
                        digests[sid] = self.rows(sid).digest()
            except Exception as e:  # noqa: BLE001 - one broken widget never stops the others
                errors[w["id"]] = str(e)[:300]
                out[w["id"]] = {"error": str(e)[:300]}
        return {"widgets": out, "digests": digests, "errors": errors}

    def widget(self, w, card, eff, k, hide, scope=""):
        t = w["type"]
        o = w.get("options") or {}
        st = w.get("style") or {}
        if t == "text":
            return {"text": str(o.get("text", ""))[:4000]}
        if t == "feed":
            return {"items": self.feed(card, int(o.get("limit") or 20))}
        if t == "filter":
            return self.filter_options(o.get("filter"), card, eff, k)
        if t == "map":
            from . import mapdata

            if mapdata.layers_of(self.d, w):
                return mapdata.compute(self, w, card, eff, k, hide)
        sid = w["source"]
        idx = self.selection(sid, card, eff)
        if t in ("kpi", "gauge"):
            v, n = self.measure(sid, w.get("measure"), idx)
            if k and n < k:
                return {"hidden": True}
            v = _round(v)
            if t == "gauge":
                hi = st.get("max", st.get("target"))
                return {"value": v, "min": _round(st.get("min", 0)), "max": _round(hi if hi is not None else
                                                                                  max(v or 0, 1))}
            last = self._last.get((w["id"], scope))
            if last is None:
                prev = None
            elif last[0] != v:
                prev = last[0]
            else:
                prev = last[1]
            self._last[(w["id"], scope)] = (v, prev)
            delta = _round(v - prev) if isinstance(v, (int, float)) and isinstance(prev, (int, float)) else None
            return {"value": v, "prev": prev, "delta": delta}
        if t == "table" and not w.get("group"):
            return self.table(sid, w, idx, k, hide)
        if t == "map":
            return self.map(sid, w, idx, k, hide)
        groups = self.groups(sid, w, idx)
        if t == "progress":
            done = self.mask(sid, o["done"])
            r = self.rows(sid)
            items = []
            for key, label, _x, gi in sorted(groups, key=lambda g: str(g[1])):
                if k and len(gi) < k:
                    items.append({"label": label, "hidden": True})
                    continue
                items.append({"key": key, "label": label, "done": sum(1 for i in gi if r.fids[i] in done),
                              "total": len(gi)})
            out = {"items": items}
            xf = _xf(w, hide)
            if xf:
                out["xf"] = xf
            return out
        if t in ("sparkline", "line", "area"):
            return self.series(sid, w, groups, k)
        if t == "choropleth":
            return self.choropleth(sid, w, groups, card, k)
        items = []
        for key, label, _x, gi in groups:
            v, n = self.measure(sid, w.get("measure"), gi)
            if k and n < k:
                items.append({"key": key, "label": label, "hidden": True, "_v": -math.inf})
            else:
                items.append({"key": key, "label": label, "value": _round(v), "hidden": False,
                              "_v": v if v is not None else -math.inf})
        g = w.get("group") or {}
        order = g.get("order") or ("key" if g.get("time") else "value")
        if order == "key":
            items.sort(key=lambda x: str(x["key"]))
        else:
            items.sort(key=lambda x: (-x["_v"], str(x["key"])))
        top = int(g.get("top") or (10 if t == "leaderboard" else 0) or 0)
        if top and len(items) > top:
            items = items[:top]
        for it in items:
            it.pop("_v", None)
        xf = _xf(w, hide)
        if t == "table":
            cols = [{"key": "key", "label": g.get("field") or "zone"}, {"key": "value", "label": w.get("title", "")}]
            rows = [{"hidden": True} if it["hidden"] else [it["label"], it.get("value")] for it in items[:MAX_ROWS]]
            return {"columns": cols, "rows": rows, "total": len(items)}
        return {"items": items, "xf": xf} if xf else {"items": items}

    def series(self, sid, w, groups, k):
        g = w.get("group") or {}
        o = w.get("options") or {}
        xkind = "time" if g.get("time") else ("number" if groups and all(x is not None for _k, _l, x, _i in groups)
                                              else "key")
        r = self.rows(sid)

        def points(gr):
            pts = []
            for key, _label, x, gi in gr:
                v, n = self.measure(sid, w.get("measure"), gi)
                xv = x if xkind != "key" else key
                pts.append([xv, None if (k and n < k) else _round(v)])
            pts.sort(key=lambda p: (p[0] if xkind != "key" else str(p[0])))
            return pts

        split = o.get("split")
        if not split or split not in r.cols:
            return {"series": [{"name": w.get("title", ""), "points": points(groups)}], "x": xkind}
        col = self.keys(r, split)
        counts = {}
        for _k, _l, _x, gi in groups:
            for i in gi:
                s = col[i]
                counts[s] = counts.get(s, 0) + 1
        names = sorted(counts, key=lambda s: (-counts[s], s))[:MAX_SERIES]
        out = []
        for s in names:
            gr = [(key, label, x, [i for i in gi if col[i] == s]) for key, label, x, gi in groups]
            out.append({"name": s or "—", "points": points([g_ for g_ in gr if g_[3]])})
        return {"series": out, "x": xkind}

    def table(self, sid, w, idx, k, hide):
        if k and len(idx) < k:
            return {"hidden": True}
        lyr = self.layer(sid)
        r = self.rows(sid)
        o = w.get("options") or {}
        fields = [f for f in (o.get("fields") or []) if f in r.cols and f not in hide]
        cols = [{"key": f, "label": lyr.fields().field(lyr.fields().indexOf(f)).displayName()} for f in fields]
        limit = max(1, min(int(o.get("limit") or MAX_ROWS), MAX_ROWS))
        rows = [[clean(r.cols[f][i]) for f in fields] for i in sorted(idx, key=lambda i: r.fids[i])[:limit]]
        return {"columns": cols, "rows": rows, "total": len(idx)}

    def _geojson(self, lyr, fids, props, tolerance=None):
        """Features of ``fids`` as GeoJSON in EPSG:4326 (simplified) and their bbox."""
        tr = None
        if lyr.crs().isValid() and lyr.crs() != WGS84:
            tr = QgsCoordinateTransform(lyr.crs(), WGS84, self.project)
        feats, box = [], QgsRectangle()
        box.setMinimal()
        if not fids:
            return {"type": "FeatureCollection", "features": []}, None
        req = QgsFeatureRequest().setFilterFids(list(fids)).setNoAttributes()
        geoms = []
        for f in lyr.getFeatures(req):
            g = QgsGeometry(f.geometry())
            if g.isEmpty():
                continue
            if tr is not None:
                try:
                    g.transform(tr)
                except QgsCsException:
                    continue
            geoms.append((f.id(), g))
            box.combineExtentWith(g.boundingBox())
        tol = tolerance
        if tol is None and not box.isEmpty():
            tol = math.hypot(box.width(), box.height()) / 3000.0
        for fid, g in geoms:
            if tol and g.type() != POINT:
                s = g.simplify(tol)
                if not s.isEmpty():
                    g = s
            try:
                geo = json.loads(g.asJson(6))
            except ValueError:
                continue
            feats.append({"type": "Feature", "id": fid, "geometry": geo, "properties": props(fid)})
        bbox = None if box.isEmpty() else [round(box.xMinimum(), 6), round(box.yMinimum(), 6),
                                           round(box.xMaximum(), 6), round(box.yMaximum(), 6)]
        return {"type": "FeatureCollection", "features": feats}, bbox

    def map(self, sid, w, idx, k, hide):
        if k and len(idx) < k:
            return {"hidden": True}
        lyr = self.layer(sid)
        r = self.rows(sid)
        o = w.get("options") or {}
        st = w.get("style") or {}
        m = w.get("measure") or {}
        if m.get("where"):
            s = self.mask(sid, m["where"])
            idx = [i for i in idx if r.fids[i] in s]
        names = [f for f in (o.get("fields") or []) if f in r.cols and f not in hide]
        if st.get("by") and st["by"] in r.cols and st["by"] not in hide and st["by"] not in names:
            names.append(st["by"])
        chosen = sorted(idx, key=lambda i: r.fids[i])
        if len(chosen) > MAX_MAP:
            step = len(chosen) / MAX_MAP
            chosen = [chosen[int(j * step)] for j in range(MAX_MAP)]
        at = {r.fids[i]: i for i in chosen}
        gj, bbox = self._geojson(lyr, list(at), lambda fid: {"id": fid, **{n: clean(r.cols[n][at[fid]])
                                                                          for n in names}})
        return {"geojson": gj, "bbox": bbox, "total": len(idx)}

    def choropleth(self, sid, w, groups, card, k):
        g = w["group"]
        zl = self.project.mapLayer(g["zones"])
        if zl is None:
            raise DataError("the zones layer is not in the project")
        values = {}
        for key, label, _x, gi in groups:
            v, n = self.measure(sid, w.get("measure"), gi)
            values[key] = (None if (k and n < k) else _round(v), bool(k and n < k), label)
        li = zl.fields().indexOf(g.get("label") or "")
        keys = {}
        for z in zl.getFeatures(QgsFeatureRequest().setFlags(NO_GEOM)):
            keys[z.id()] = key_of(z.attributes()[li]) if li >= 0 else str(z.id())
        allowed = set(keys)
        if (card or {}).get("zone"):
            allowed = self._zone_fids(zl, card["zone"])

        def props(fid):
            key = keys.get(fid, "")
            v, hid, label = values.get(key, (None, False, key))
            p = {"key": key, "label": label, "value": v}
            if hid:
                p["hidden"] = True
            return p
        gj, bbox = self._geojson(zl, sorted(allowed), props)
        vals = [v[0] for kk, v in values.items() if v[0] is not None]
        return {"geojson": gj, "bbox": bbox, "min": min(vals) if vals else None, "max": max(vals) if vals else None}

    def filter_options(self, filter_id, card, eff, k):
        f = next((x for x in self.d.get("filters") or [] if x.get("id") == filter_id), None)
        if f is None:
            raise DataError("no such filter")
        if f.get("field") in set((card or {}).get("hide") or ()):
            raise DataError("hidden")
        sid = f["source"]
        forced = ((card or {}).get("filters") or {}).get(filter_id)
        idx = self.selection(sid, card, eff, skip=filter_id)
        r = self.rows(sid)
        col = self.keys(r, f["field"]) if f["field"] in r.cols else []
        counts = {}
        for i in idx:
            kk = col[i]
            counts[kk] = counts.get(kk, 0) + 1
        if forced is not None:
            allowed = {str(v) for v in (forced if isinstance(forced, list) else [forced])}
            counts = {kk: v for kk, v in counts.items() if kk in allowed}
        opts = [{"key": kk, "label": kk if kk != "" else "—", "count": (None if (k and n < k) else n)}
                for kk, n in sorted(counts.items(), key=lambda x: str(x[0]))][:MAX_OPTIONS]
        out = {"options": opts, "current": eff.get(filter_id, [])}
        out["xf"] = f["field"]      # the page's cross-filter: one chip on selects its features on the maps
        if forced is not None:
            out["locked"] = True
        return out

    # ---- the edits feed ------------------------------------------------------------------------------------------
    def _feed_room(self):
        try:
            from .. import team

            node = team.current()
        except Exception:  # noqa: BLE001
            node = None
        if node is None or getattr(node, "store", None) is None:
            return None
        items = []
        for op in node.store.last(200):
            if op.get("kind") == "field":
                continue
            lyr = (getattr(node, "layers", {}) or {}).get(op.get("layer"))
            if lyr is None:
                continue
            items.append({"ts": op.get("ts"), "who": op.get("name") or "", "kind": _kind(op), "fid": op.get("fid"),
                          "layer": lyr.name(), "layer_id": lyr.id()})
        return items

    def feed(self, card, limit=20):
        items = self._feed_room()
        if items is None:
            items = list(self.edit_log)
        src_layers = {s.get("layer"): sid for sid, s in (self.d.get("sources") or {}).items()}
        zone = (card or {}).get("zone")
        out = []
        for it in reversed(items):
            sid = src_layers.get(it.get("layer_id"))
            if sid is None:
                continue        # only the dashboard's own layers
            if zone and it.get("kind") != "del":
                try:
                    if it.get("fid") not in self.zone(sid, zone):
                        continue
                except DataError:
                    continue
            elif zone:
                continue        # a deleted feature cannot be placed: not shown to a card with a zone
            verb = {"add": "added", "edit": "edited", "del": "deleted"}.get(it.get("kind"), "edited")
            out.append({"ts": it.get("ts"), "who": it.get("who", ""), "what": f"{verb} #{it.get('fid')}",
                        "layer": it.get("layer", ""), "kind": it.get("kind"), "fid": it.get("fid")})
            if len(out) >= limit:
                break
        return out

    def log_edit(self, layer, kind, fid, who=""):
        self.edit_log.append({"ts": __import__("time").time(), "who": who, "kind": kind, "fid": fid,
                              "layer": layer.name(), "layer_id": layer.id()})
        del self.edit_log[:-500]

    # ---- what is behind a number -----------------------------------------------------------------------------
    def explain(self, wid, key, card=None, filters=None):
        w = next((x for x in self.d.get("widgets") or [] if x.get("id") == wid), None)
        if w is None or (card is not None and wid not in model.allowed(self.d, card)):
            raise DataError("no such widget")
        if w.get("type") == "map":
            from . import mapdata

            if mapdata.layers_of(self.d, w):
                self._sel = {}
                hide = set((card or {}).get("hide") or ())
                return mapdata.explain(self, w, key, card, self.effective_filters(card, filters),
                                       int(self.d.get("k_min") or 0), hide)
        if not w.get("source"):
            raise DataError("no such widget")
        self._sel = {}
        sid = w["source"]
        eff = self.effective_filters(card, filters)
        idx = self.selection(sid, card, eff)
        r = self.rows(sid)
        m = w.get("measure") or {}
        if m.get("where"):
            s = self.mask(sid, m["where"])
            idx = [i for i in idx if r.fids[i] in s]
        if w.get("group") and key is not None:
            idx = next((gi for kk, _l, _x, gi in self.groups(sid, w, idx) if str(kk) == str(key)), [])
        k = int(self.d.get("k_min") or 0)
        out = {"t": "explain", "widget": wid, "key": key, "count": len(idx)}
        if k and len(idx) < k:
            out.update(hidden=True, features=[], bbox=None, more=False)
            return out
        lyr = self.layer(sid)
        hide = set((card or {}).get("hide") or ())
        label = (w.get("options") or {}).get("label")
        col = r.cols.get(label) if label and label not in hide else None
        chosen = sorted(idx, key=lambda i: r.fids[i])
        _gj, bbox = self._geojson(lyr, [r.fids[i] for i in chosen[:2000]], lambda fid: {})
        hist = self._history(lyr)
        feats = []
        for i in chosen[:MAX_EXPLAIN]:
            fid = r.fids[i]
            by, when = hist(fid)
            feats.append({"id": fid, "label": clean(col[i]) if col is not None else f"#{fid}", "by": by,
                          "when": when})
        out.update(bbox=bbox, features=feats, more=len(chosen) > MAX_EXPLAIN)
        return out

    def _history(self, lyr):
        try:
            from .. import team

            node = team.current()
            key = node.key_of(lyr) if node is not None else None
        except Exception:  # noqa: BLE001
            node, key = None, None
        if node is None or not key:
            log = {it["fid"]: it for it in self.edit_log if it.get("layer_id") == lyr.id()}
            return lambda fid: ((log[fid].get("who") or None, log[fid].get("ts")) if fid in log else (None, None))

        def h(fid):
            try:
                ops = node.store.feature_history(key, fid)
            except Exception:  # noqa: BLE001
                return None, None
            if not ops:
                return None, None
            return ops[-1].get("name") or None, ops[-1].get("ts")
        return h


def _xf(w, hide):
    from .mapdata import chart_xf

    return chart_xf(w, hide)


def _kind(op):
    if op.get("kind") == "del":
        return "del"
    return "add" if op.get("prev") is None and int(op.get("ver") or 0) <= 1 else "edit"
