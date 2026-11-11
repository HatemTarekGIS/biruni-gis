"""Spatial rules: what the shapes of a layer keep, learned from the data, checked on every save, kept in the data.

The charter (charter/) keeps what the values of the fields must be. These are the rules of the shapes, which no
form can check: features do not overlap, a parcel is never smaller than so much, the area field is the parcel's
area, every building is inside a parcel, line ends meet other lines.

* learn: each kind of rule is tried on the data as it is; a rule that the data keeps almost everywhere is
  proposed, with how often it holds and the features that break it now (plain counting and the median, no model).
* check: only the features added or changed are checked, against their neighbours, so a save of a few
  features in a layer of a hundred thousand takes no time.
* keep: in the data itself - a ``biruni_rules`` table inside a GeoPackage, a ``.biruni-rules.json`` file beside a
  shapefile - so the rules go wherever the data goes, and any Biruni that opens it keeps them.
"""
import datetime as dt
import json
import math
import os
import sqlite3
import statistics

from qgis.core import (QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsFeatureRequest, QgsGeometry, QgsPointXY,
                       QgsProject, QgsProviderRegistry, QgsRectangle, QgsSpatialIndex, QgsVectorLayer, QgsWkbTypes)

FORMAT = 1
KIND = "biruni-spatial-rules"
PROP = "biruni/georules"
TABLE = "biruni_rules"
SIDECAR = ".biruni-rules.json"
LEARN_LIMIT = 20000
HOLDS = {"valid": 0.99, "no_overlap": 0.97, "min_area": 0.97, "measure_field": 0.95, "inside": 0.97,
         "ends_meet": 0.95}


# ---- measuring ----------------------------------------------------------------------------------------------
def _unit(layer):
    """Layer units in a metre (degrees: about 1/111 320)."""
    return 1.0 / 111320.0 if layer.crs().isGeographic() else 1.0


def _da(layer):
    da = QgsDistanceArea()
    da.setSourceCrs(layer.crs(), QgsProject.instance().transformContext())
    da.setEllipsoid("EPSG:7030" if layer.crs().isGeographic() else "NONE")
    return da


def _kind(layer):
    return QgsWkbTypes.geometryType(layer.wkbType())


def _empty(g):
    return g is None or g.isNull() or g.isEmpty()


def _polygonal(g):
    if _empty(g):
        return QgsGeometry()
    if g.type() == QgsWkbTypes.PolygonGeometry:
        return g
    if QgsWkbTypes.flatType(g.wkbType()) == QgsWkbTypes.GeometryCollection:
        parts = [QgsGeometry(p.clone()) for p in g.constGet()]
        parts = [p for p in parts if p.type() == QgsWkbTypes.PolygonGeometry]
        return QgsGeometry.unaryUnion(parts) if parts else QgsGeometry()
    return QgsGeometry()


def nice_floor(v):
    """Down to two figures: 163.4 -> 160, 0.874 -> 0.87."""
    if v <= 0:
        return 0.0
    e = 10 ** (math.floor(math.log10(v)) - 1)
    return math.floor(v / e) * e


def _nice(v):
    return float(f"{v:.6g}")


# ---- the rules ------------------------------------------------------------------------------------------------
def rule(kind, level="error", **params):
    rid = kind + ("" if not params else ":" + ",".join(f"{k}={params[k]}" for k in sorted(params)))
    return {"id": rid, "kind": kind, "level": level, "params": params}


class Violation:
    def __init__(self, fid, rule_, text_key, values, geom=None):
        self.fid, self.rule, self.key, self.values, self.geom = fid, rule_, text_key, values, geom

    def __repr__(self):
        return f"Violation({self.fid}, {self.rule['kind']}, {self.values})"


class Checker:
    """The rules of one layer, checked on some of its features (or all of them)."""

    def __init__(self, layer, rules, project=None):
        self.layer, self.rules = layer, [r for r in rules if r.get("on", True)]
        self.project = project or QgsProject.instance()
        self.da = _da(layer)
        self.unit = _unit(layer)
        self.missing = []     # rules that cannot be checked here (a layer they name is not on the map)

    def _others(self, name):
        found = [l for l in self.project.mapLayersByName(name) if isinstance(l, QgsVectorLayer) and l.isValid()
                 and l.id() != self.layer.id()]
        return found[0] if found else None

    def _near(self, layer, rect):
        return {f.id(): f for f in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect))}

    def check(self, fids=None, levels=("error", "warning")):
        """[Violation] for the features ``fids`` (None: every feature)."""
        lyr = self.layer
        if fids is None:
            feats = list(lyr.getFeatures())
        else:      # features not saved yet (ids below 0) from the edits: a file is not asked for them
            buf = lyr.editBuffer()
            new = buf.addedFeatures() if buf is not None else {}
            feats = [QgsFeature(new[f]) for f in fids if f < 0 and f in new]
            old = [f for f in fids if f >= 0]
            if old:
                feats += list(lyr.getFeatures(QgsFeatureRequest().setFilterFids(old)))
        out = []
        seen_pairs = set()
        for r in self.rules:
            if r.get("level", "error") not in levels:
                continue
            k, p = r["kind"], r.get("params", {})
            if k == "inside":
                other = self._others(p["layer"])
                if other is None:
                    self.missing.append(r)
                    continue
                to_other = QgsCoordinateTransform(lyr.crs(), other.crs(), self.project) \
                    if other.crs() != lyr.crs() else None
                tol = float(p.get("tol", 0.1)) * _unit(other)
            if k == "measure_field":
                fi = lyr.fields().indexOf(p["field"])
                if fi < 0:
                    self.missing.append(r)
                    continue
            for f in feats:
                g = f.geometry()
                if k == "valid":
                    if not _empty(g) and not g.isGeosValid():
                        out.append(Violation(f.id(), r, "invalid", {}, g))
                elif k == "no_overlap":
                    if _empty(g):
                        continue
                    gv = g if g.isGeosValid() else g.makeValid()
                    eng = QgsGeometry.createGeometryEngine(gv.constGet())
                    eng.prepareGeometry()
                    for oid, of in self._near(lyr, g.boundingBox()).items():
                        if oid == f.id() or _empty(of.geometry()):
                            continue
                        pair = (min(oid, f.id()), max(oid, f.id()))
                        if fids is None and pair in seen_pairs:
                            continue
                        og = of.geometry() if of.geometry().isGeosValid() else of.geometry().makeValid()
                        if not eng.intersects(og.constGet()):
                            continue
                        inter = _polygonal(gv.intersection(og))
                        a = self.da.measureArea(inter) if not _empty(inter) else 0.0
                        if a > float(p.get("min_area", 0.01)):
                            seen_pairs.add(pair)
                            out.append(Violation(f.id(), r, "overlap", {"other": oid, "area": round(a, 3)}, inter))
                elif k == "min_area":
                    if _empty(g):
                        continue
                    a = self.da.measureArea(g)
                    if a < float(p["min"]):
                        out.append(Violation(f.id(), r, "small", {"area": round(a, 3), "min": p["min"]}, g))
                elif k == "measure_field":
                    v = f.attribute(fi)
                    if v is None or _empty(g):
                        continue
                    try:
                        v = float(v)
                    except (TypeError, ValueError):
                        continue
                    m = self.da.measureArea(g) if p.get("what", "area") == "area" else self.da.measureLength(g)
                    if abs(v - m) > max(float(p.get("abs", 0.5)), float(p.get("rel", 0.01)) * m):
                        out.append(Violation(f.id(), r, "measure", {"field": p["field"], "value": v,
                                                                     "measured": round(m, 3)}, g))
                elif k == "inside":
                    if _empty(g):
                        continue
                    gg = QgsGeometry(g)
                    if to_other is not None:
                        gg.transform(to_other)
                    cands = [of.geometry() for of in self._near(other, gg.boundingBox()).values()
                             if not _empty(of.geometry())]
                    ok = any(c.contains(gg) for c in cands)
                    if not ok and cands:
                        ok = QgsGeometry.unaryUnion(cands).buffer(tol, 4).contains(gg)
                    if not ok:
                        out.append(Violation(f.id(), r, "outside", {"layer": p["layer"]}, g))
                elif k == "ends_meet":
                    if _empty(g):
                        continue
                    tol = float(p.get("tol", 0.05)) * self.unit
                    for pt in _ends(g):
                        if not self._end_meets(f.id(), g, pt, tol):
                            out.append(Violation(f.id(), r, "dangle", {}, QgsGeometry.fromPointXY(pt)))
        return out

    def _end_meets(self, fid, g, pt, tol):
        rect = QgsRectangle(pt.x() - tol, pt.y() - tol, pt.x() + tol, pt.y() + tol)
        pg = QgsGeometry.fromPointXY(pt)
        for oid, of in self._near(self.layer, rect).items():
            og = of.geometry()
            if _empty(og):
                continue
            if oid == fid:          # the line itself, further along: a loop, or a T on itself
                if _touches_self(og, pt, tol):
                    return True
                continue
            if og.distance(pg) <= tol:
                return True
        return False


def _ends(g):
    pts = []
    for part in (g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]):
        if len(part) >= 2 and part[0].distance(part[-1]) > 0:
            pts += [QgsPointXY(part[0]), QgsPointXY(part[-1])]
    return pts


def _touches_self(g, pt, tol):
    """The end of a line meets the line itself further along (a loop or a T on itself)."""
    for part in (g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]):
        if len(part) < 3:
            continue
        for ends, rest in ((part[0], part[2:]), (part[-1], part[:-2])):
            if QgsPointXY(ends).distance(pt) <= tol and len(rest) >= 2:
                if QgsGeometry.fromPolylineXY([QgsPointXY(q) for q in rest]).distance(
                        QgsGeometry.fromPointXY(pt)) <= tol:
                    return True
    return False


# ---- learning ----------------------------------------------------------------------------------------------
class Proposal:
    def __init__(self, rule_, holds, exceptions, n):
        self.rule, self.holds, self.exceptions, self.n = rule_, holds, list(exceptions), n


def learn(layer, project=None, feedback=None):
    """[Proposal] of the rules the data keeps almost everywhere."""
    project = project or QgsProject.instance()
    feats = []
    for i, f in enumerate(layer.getFeatures()):
        if i >= LEARN_LIMIT:
            break
        feats.append(f)
    n = len(feats)
    if n < 10:
        return []
    fids = [f.id() for f in feats]
    kind = _kind(layer)
    da = _da(layer)
    out = []

    def propose(r, bad):
        holds = 1.0 - len(bad) / n
        if holds >= HOLDS[r["kind"]]:
            out.append(Proposal(r, holds, bad, n))

    def run(r):
        return {v.fid for v in Checker(layer, [r], project).check(fids)}

    if kind == QgsWkbTypes.PolygonGeometry:
        propose(rule("valid"), run(rule("valid")))
        r = rule("no_overlap", min_area=0.01)
        propose(r, run(r))
        areas = {f.id(): da.measureArea(f.geometry()) for f in feats if not _empty(f.geometry())}
        pos = [a for a in areas.values() if a > 0]
        if len(pos) >= 10:
            logs = [math.log(a) for a in pos]
            med = statistics.median(logs)
            mad = statistics.median(abs(x - med) for x in logs) * 1.4826 or 0.05
            low = math.exp(med - 5 * mad)
            bad = {fid for fid, a in areas.items() if a < low}
            keep = [a for fid, a in areas.items() if fid not in bad]
            m = nice_floor(min(low, min(keep))) if keep else 0   # the data's lower bound, not its smallest
            if m >= 0.5:
                propose(rule("min_area", min=_nice(m)), bad)
    if kind in (QgsWkbTypes.PolygonGeometry, QgsWkbTypes.LineGeometry):
        what = "area" if kind == QgsWkbTypes.PolygonGeometry else "length"
        for fl in layer.fields():
            if not fl.isNumeric():
                continue
            vals = [f.attribute(fl.name()) for f in feats]
            if sum(v is not None for v in vals) < 0.8 * n:
                continue
            r = rule("measure_field", field=fl.name(), what=what, rel=0.01, abs=0.5)
            bad = run(r)
            if 1.0 - len(bad) / n >= HOLDS["measure_field"]:
                out.append(Proposal(r, 1.0 - len(bad) / n, bad, n))
    if kind == QgsWkbTypes.LineGeometry:
        r = rule("ends_meet", tol=0.05)
        propose(r, run(r))
    ext = layer.extent()
    for other in project.mapLayers().values():
        if not isinstance(other, QgsVectorLayer) or not other.isValid() or other.id() == layer.id():
            continue
        if _kind(other) != QgsWkbTypes.PolygonGeometry or other.featureCount() == 0:
            continue
        if len(project.mapLayersByName(other.name())) != 1:
            continue
        oext = other.extent()
        if other.crs() != layer.crs():
            oext = QgsCoordinateTransform(other.crs(), layer.crs(), project).transformBoundingBox(oext)
        if not oext.intersects(ext):
            continue
        r = rule("inside", layer=other.name(), tol=0.1)
        propose(r, run(r))
    return out


# ---- keeping the rules in the data ---------------------------------------------------------------------------
def _source(layer):
    """(path, layer name inside the file, provider) of a layer kept in a file, else (None, None, None)."""
    if not isinstance(layer, QgsVectorLayer) or layer.providerType() != "ogr":
        return None, None, None
    parts = QgsProviderRegistry.instance().decodeUri("ogr", layer.source())
    path = parts.get("path") or ""
    if not path or not os.path.isfile(path):
        return None, None, None
    name = parts.get("layerName") or os.path.splitext(os.path.basename(path))[0]
    return path, name, "gpkg" if path.lower().endswith(".gpkg") else "file"


def document(layer, rules):
    return {"format": FORMAT, "kind": KIND, "layer": layer.name(),
            "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "rules": rules}


def _gpkg_read(path, name):
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(f"SELECT rules FROM {TABLE} WHERE layer=?", (name,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return json.loads(row[0]) if row else None


def _gpkg_write(path, name, doc):
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    con = sqlite3.connect(path, timeout=10)
    try:
        with con:
            con.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (layer TEXT PRIMARY KEY NOT NULL, rules TEXT NOT NULL, "
                        "updated TEXT)")
            if doc is None:
                con.execute(f"DELETE FROM {TABLE} WHERE layer=?", (name,))
            else:
                con.execute(f"INSERT OR REPLACE INTO {TABLE} (layer, rules, updated) VALUES (?, ?, ?)",
                            (name, json.dumps(doc, ensure_ascii=False), now))
            if con.execute("SELECT 1 FROM gpkg_contents WHERE table_name=?", (TABLE,)).fetchone() is None:
                con.execute("INSERT INTO gpkg_contents (table_name, data_type, identifier, description, last_change) "
                            "VALUES (?, 'attributes', ?, ?, ?)",
                            (TABLE, TABLE, "Biruni: the spatial rules the layers of this file keep", now))
    finally:
        con.close()


def sidecar_path(path, name):
    return os.path.join(os.path.dirname(path), name + SIDECAR)


def load(layer):
    """The rules document of a layer: from the project, else from its file. None when it has none."""
    raw = layer.customProperty(PROP, "")
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    path, name, how = _source(layer)
    if how == "gpkg":
        return _gpkg_read(path, name)
    if how == "file":
        sc = sidecar_path(path, name)
        if os.path.isfile(sc):
            try:
                with open(sc, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                return None
    return None


def save(layer, rules):
    """Keep the rules with the layer and in its file. Returns where: "gpkg", "file" or "project"."""
    doc = document(layer, rules) if rules else None
    layer.setCustomProperty(PROP, json.dumps(doc, ensure_ascii=False) if doc else "")
    path, name, how = _source(layer)
    if how == "gpkg":
        _gpkg_write(path, name, doc)
    elif how == "file":
        sc = sidecar_path(path, name)
        if doc is None:
            if os.path.isfile(sc):
                os.remove(sc)
        else:
            with open(sc, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, indent=1)
    return how or "project"
