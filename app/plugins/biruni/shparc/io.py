"""A shapefile that remembers its arcs.

``export`` writes an ordinary shapefile every program opens: an arc is short lines that keep the arc's area
exactly and its length to a tenth of a millimetre (arcs.py). Beside it goes one small file, ``<name>.crv``,
with the true shape of every feature that had arcs - so ``restore`` gives the arcs back, as arcs.

The .crv file (JSON, see SPEC.md) knows a feature by the fingerprint of the short lines written for it, not by
its number: when another program changes that feature its fingerprint no longer matches, the arcs kept for it
are left out and the feature is taken as that program left it. Records deleted or put in another order by
another program do not confuse it.
"""
import datetime as dt
import hashlib
import json
import os

from qgis.core import (QgsCircularString, QgsCompoundCurve, QgsCurvePolygon, QgsFeature, QgsField, QgsFields,
                       QgsGeometry, QgsGeometryCollection, QgsLineString, QgsMemoryProviderUtils, QgsMultiCurve,
                       QgsMultiLineString, QgsMultiPolygon, QgsMultiSurface, QgsPoint, QgsPolygon, QgsProject,
                       QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from . import arcs

FORMAT = "biruni-shapefile-arcs"
VERSION = 1
EXT = ".crv"
AREA_FIELD, PERIMETER_FIELD, LENGTH_FIELD = "AREA_TRUE", "PERIM_TRUE", "LEN_TRUE"


class Stats:
    def __init__(self):
        self.arcs = 0
        self.segments = 0
        self.max_length_diff = 0.0
        self.not_rounded = 0     # arcs whose length could not be kept to the centimetre

    def add(self, info, round_to):
        if info["r"] is None:
            return
        self.arcs += 1
        self.segments += info["n"]
        self.max_length_diff = max(self.max_length_diff, abs(info["lines"] - info["arc"]))
        if round_to is not None and round(info["lines"], round_to) != round(info["arc"], round_to):
            self.not_rounded += 1


def _points(curve, stats, tolerance, round_to):
    """A curve as the points of its short lines."""
    if isinstance(curve, QgsCompoundCurve):
        out = []
        for i in range(curve.nCurves()):
            pts = _points(curve.curveAt(i), stats, tolerance, round_to)
            out += pts if not out else pts[1:]
        return out
    if isinstance(curve, QgsCircularString):
        src = curve.points()
        if len(src) < 3:
            return list(src)
        z = curve.is3D()
        out = [QgsPoint(src[0].x(), src[0].y(), src[0].z()) if z else QgsPoint(src[0].x(), src[0].y())]
        for i in range(0, len(src) - 2, 2):
            p0, pm, p1 = src[i], src[i + 1], src[i + 2]
            xy, info = arcs.arc_points((p0.x(), p0.y()), (pm.x(), pm.y()), (p1.x(), p1.y()), tolerance, round_to)
            stats.add(info, round_to)
            n = len(xy) - 1
            for k, (x, y) in enumerate(xy[1:], 1):
                if z:
                    out.append(QgsPoint(x, y, p0.z() + (p1.z() - p0.z()) * k / n))
                else:
                    out.append(QgsPoint(x, y))
        return out
    return list(curve.points())


def _linear(g, stats, tolerance, round_to):
    if isinstance(g, QgsCurvePolygon):   # QgsPolygon too: its rings are lines already
        out = QgsPolygon()
        if g.exteriorRing() is not None:
            out.setExteriorRing(QgsLineString(_points(g.exteriorRing(), stats, tolerance, round_to)))
        for i in range(g.numInteriorRings()):
            out.addInteriorRing(QgsLineString(_points(g.interiorRing(i), stats, tolerance, round_to)))
        return out
    if isinstance(g, QgsGeometryCollection):
        kind = QgsMultiPolygon if isinstance(g, QgsMultiSurface) else QgsMultiLineString if isinstance(g, QgsMultiCurve) \
            else None
        if kind is None:
            return g.clone()
        out = kind()
        for i in range(g.numGeometries()):
            out.addGeometry(_linear(g.geometryN(i), stats, tolerance, round_to))
        return out
    if isinstance(g, (QgsCompoundCurve, QgsCircularString)):
        return QgsLineString(_points(g, stats, tolerance, round_to))
    return g.clone()


def linear(geom, tolerance=arcs.LENGTH_TOLERANCE, round_to=arcs.ROUND_TO, stats=None):
    """``geom`` with its arcs as short lines of the same area -> (QgsGeometry, Stats)."""
    stats = stats or Stats()
    if geom is None or geom.isNull() or not geom.constGet().hasCurvedSegments():
        return geom, stats
    return QgsGeometry(_linear(geom.constGet(), stats, tolerance, round_to)), stats


def fingerprint(geom):
    return hashlib.sha1(bytes(geom.asWkb())).hexdigest()


def sidecar(path):
    return os.path.splitext(path)[0] + EXT


def remembers(path):
    """The shapefile at ``path`` has arcs kept beside it."""
    side = sidecar(path)
    if not path.lower().endswith(".shp") or not os.path.isfile(side):
        return False
    try:
        with open(side, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("format") == FORMAT and bool(d.get("arcs") or d.get("fields"))
    except (OSError, ValueError):
        return False


def export(layer, path, only_selected=False, measures=True, tolerance=arcs.LENGTH_TOLERANCE, round_to=arcs.ROUND_TO):
    """Write ``layer`` as a shapefile at ``path`` with its arcs kept in ``<name>.crv``.

    -> {"features", "curved", "arcs", "max_length_diff", "max_area_diff", "not_rounded", "fields" (names that
    were shortened), "sidecar" (path or None), "error" (text or "")}"""
    report = {"features": 0, "curved": 0, "arcs": 0, "max_length_diff": 0.0, "max_area_diff": 0.0, "not_rounded": 0,
              "fields": {}, "sidecar": None, "error": ""}
    kind = QgsWkbTypes.geometryType(layer.wkbType())
    polygon, line = kind == QgsWkbTypes.GeometryType.PolygonGeometry, kind == QgsWkbTypes.GeometryType.LineGeometry
    fields = QgsFields(layer.fields())
    extra = []
    if measures and (polygon or line):
        for name in ((AREA_FIELD, PERIMETER_FIELD) if polygon else (LENGTH_FIELD,)):
            if fields.indexOf(name) < 0:
                fields.append(QgsField(name, QMetaType.Type.Double, "double", 24, 4))
                extra.append(name)
    wkb = QgsWkbTypes.dropM(QgsWkbTypes.linearType(layer.wkbType()))
    if polygon or line:
        wkb = QgsWkbTypes.multiType(wkb)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "ESRI Shapefile"
    opts.fileEncoding = "UTF-8"
    opts.layerOptions = ["ENCODING=UTF-8"]
    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg", EXT):   # a clean start: no arcs of an older file
        old = os.path.splitext(path)[0] + ext
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError as e:
                report["error"] = str(e)
                return report
    writer = QgsVectorFileWriter.create(path, fields, wkb, layer.crs(), QgsProject.instance().transformContext(), opts)
    if writer.hasError() != QgsVectorFileWriter.WriterError.NoError:
        report["error"] = writer.errorMessage()
        return report
    stats, curved = Stats(), {}     # record number -> the true shape (WKT)
    request = layer.getSelectedFeatures() if only_selected else layer.getFeatures()
    for n, f in enumerate(request):
        g = f.geometry()
        out = QgsFeature(fields)
        attrs = list(f.attributes())
        lin = g
        if g is not None and not g.isNull():
            if g.constGet().hasCurvedSegments():
                lin, _s = linear(g, tolerance, round_to, stats)
                curved[n] = g.asWkt(17)
                if polygon:
                    report["max_area_diff"] = max(report["max_area_diff"], abs(lin.area() - g.area()))
            if polygon and extra:
                attrs += [g.area(), g.length()][:len(extra)]
            elif line and extra:
                attrs += [g.length()]
            out.setGeometry(lin)
        elif extra:
            attrs += [None] * len(extra)
        out.setAttributes(attrs)
        if not writer.addFeature(out):
            report["error"] = writer.errorMessage()
            break
        report["features"] += 1
    del writer   # the file is closed
    if report["error"]:
        return report
    report.update(curved=len(curved), arcs=stats.arcs, max_length_diff=stats.max_length_diff, not_rounded=stats.not_rounded)
    # what was really written: the fingerprints of the short lines, and the field names the format shortened
    back = QgsVectorLayer(path, "written", "ogr")
    kept = {}
    if back.isValid():
        for n, f in enumerate(back.getFeatures()):
            if n in curved and f.hasGeometry():
                kept[fingerprint(f.geometry())] = curved[n]
        names = [x.name() for x in back.fields()]
        report["fields"] = {short: full.name() for short, full in zip(names, fields) if short != full.name()}
    del back
    if kept or report["fields"]:
        doc = {"format": FORMAT, "version": VERSION,
               "about": "The true shapes (with arcs) of features of the shapefile of the same name. A feature is "
                        "known by the SHA-1 of the WKB of its shape as stored in the .shp; a feature another "
                        "program changed no longer matches and is taken as it is.",
               "written": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "length_tolerance": tolerance, "fields": report["fields"], "arcs": kept}
        with open(sidecar(path), "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        report["sidecar"] = sidecar(path)
    return report


def restore(path, name=None):
    """The shapefile at ``path`` as a layer with its arcs back (a memory layer; the shapefile is not changed).

    -> (layer or None, {"features", "restored", "changed"}): ``restored`` features got their arcs back,
    ``changed`` were altered by another program since (their arcs are no longer known)."""
    report = {"features": 0, "restored": 0, "changed": 0}
    shp = QgsVectorLayer(path, name or os.path.splitext(os.path.basename(path))[0], "ogr")
    if not shp.isValid():
        return None, report
    try:
        with open(sidecar(path), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    known = doc.get("arcs") or {} if doc.get("format") == FORMAT else {}
    renamed = doc.get("fields") or {} if doc.get("format") == FORMAT else {}
    fields = QgsFields()
    for fld in shp.fields():
        fld = QgsField(fld)
        fld.setName(renamed.get(fld.name(), fld.name()))
        fields.append(fld)
    wkb = QgsWkbTypes.curveType(QgsWkbTypes.multiType(shp.wkbType())) if known else shp.wkbType()
    out = QgsMemoryProviderUtils.createMemoryLayer(shp.name(), fields, wkb, shp.crs())
    pr = out.dataProvider()
    feats, used = [], set()
    for f in shp.getFeatures():
        g = f.geometry()
        new = QgsFeature(out.fields())
        new.setAttributes(f.attributes())
        if f.hasGeometry():
            mark = fingerprint(g)
            wkt = known.get(mark)
            if wkt:
                true = QgsGeometry.fromWkt(wkt)
                if not true.isNull():
                    g = true
                    used.add(mark)
                    report["restored"] += 1
            fit = pr.convertToProviderType(g)
            new.setGeometry(fit if fit is not None and not fit.isNull() else g)
        feats.append(new)
    pr.addFeatures(feats)
    out.updateExtents()
    report["features"] = len(feats)
    report["changed"] = len(set(known) - used)
    return out, report
