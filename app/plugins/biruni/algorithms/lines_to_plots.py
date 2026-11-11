"""Plots from the loose lines of a CAD plan.

CAD plans are rarely drawn as closed shapes: plot sides are separate lines,
shared sides are drawn twice, line ends stop just short of the next line or
run just past it, and some sides are arcs. This tool repairs the linework the
way a drafter would - join ends that nearly meet, extend short lines to the
line they were meant to reach, ignore overshoots and duplicates - then builds a
polygon for every enclosed area and reads the plot number written inside it.
Every place where a plot could not be closed is reported as a point, so the
user knows exactly where to look.
"""
import math
import re

from qgis.core import (Qgis, QgsCoordinateTransform, QgsDistanceArea, QgsFeature, QgsFeatureSink, QgsField,
                       QgsFields, QgsGeometry, QgsPointXY, QgsProcessingException,
                       QgsProcessingParameterBoolean, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterMultipleLayers, QgsProcessingParameterNumber, QgsRectangle,
                       QgsSpatialIndex, QgsWkbTypes)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")
# a side length or an area written inside a plot is not its number: "20.00", "٢٠٫٥", "600 م2", "15م"
_LENGTH_LIKE = re.compile(r"^\s*\d+[.,]\d+\s*$|[مm]\s*[2²]?\s*$|^\s*[مm]\b|²|\bم2\b|\bm2\b", re.IGNORECASE)
_NUMBER_FIELDS = ("plot_no", "plot", "parcel", "number", "no", "num", "label", "text", "txt", "رقم", "القطعة")
NODE_DIGITS = 4          # node identity: coordinates rounded to 0.1 mm (map units)
STRAIGHT_DEG = 1.0       # a turn below this is not a corner
ARC_DEG = 12.0           # consecutive small turns of one sign are one arc side


def normalise_digits(text):
    return str(text or "").translate(_DIGITS).strip()


def is_length_like(text):
    return bool(_LENGTH_LIKE.search(normalise_digits(text)))


def number_candidates(texts):
    """The texts that can be a plot number: they have a digit and are not a length or an area."""
    return [t for t in texts if re.search(r"\d", normalise_digits(t)) and not is_length_like(t)]


def _key(p):
    return round(p.x(), NODE_DIGITS), round(p.y(), NODE_DIGITS)


def _turn(a, b, c):
    """Signed turn angle in degrees at b going a -> b -> c (left positive)."""
    ang1 = math.atan2(b.y() - a.y(), b.x() - a.x())
    ang2 = math.atan2(c.y() - b.y(), c.x() - b.x())
    d = math.degrees(ang2 - ang1)
    while d > 180:
        d -= 360
    while d <= -180:
        d += 360
    return d


def side_lengths(ring, measure):
    """Lengths of the sides of a closed ring, clockwise from its northernmost corner. Straight runs are
    one side; a run of small same-direction turns (an arc drawn as segments) is one side too."""
    pts = [QgsPointXY(p) for p in ring]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return []
    area2 = sum(pts[i].x() * pts[(i + 1) % len(pts)].y() - pts[(i + 1) % len(pts)].x() * pts[i].y()
                for i in range(len(pts)))
    if area2 > 0:  # counter-clockwise -> clockwise
        pts.reverse()
    changed = True
    while changed and len(pts) > 3:  # drop straight-through vertices
        changed = False
        for i in range(len(pts)):
            a, b, c = pts[i - 1], pts[i], pts[(i + 1) % len(pts)]
            if abs(_turn(a, b, c)) < STRAIGHT_DEG or b == a:
                del pts[i]
                changed = True
                break
    n = len(pts)
    turns = [_turn(pts[i - 1], pts[i], pts[(i + 1) % n]) for i in range(n)]
    # vertices that are real corners start a side; arc vertices (small turns) continue it
    corner = [abs(t) > ARC_DEG for t in turns]
    if not any(corner):  # a circle or a smooth shape: one side
        corner[0] = True
    top = max((i for i in range(n) if corner[i]), key=lambda i: (pts[i].y(), -pts[i].x()))
    order = [(top + k) % n for k in range(n)]
    sides, current = [], 0.0
    for k, i in enumerate(order):
        j = order[(k + 1) % n]
        current += measure(pts[i], pts[j])
        if corner[j]:
            sides.append(current)
            current = 0.0
    if current:
        sides.append(current)
    return sides


class LinesToPlots(BiruniAlgorithm):
    """Loose CAD linework -> closed plots with their numbers, areas and side lengths."""

    ID = "linestoplots"
    NAME = "Plots from loose lines"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Turns the lines of a plan - from AutoCAD or any drawing, where plot sides are separate lines - into "
            "closed plots. Ends that nearly meet are joined, short lines are extended to the line they were "
            "meant to reach, overshoots and duplicated lines are ignored and arcs are kept. Each enclosed area "
            "becomes a plot with its number (read from the texts inside it; lengths such as 20.00 and areas are "
            "skipped), area, perimeter, centroid X/Y and side lengths, clockwise from the north. Every place "
            "where a plot could not be closed is reported in the Problems layer with the size of the gap. "
            "Lengths and areas are measured as drawn (grid) in a projected coordinate system.")

    INPUT, TEXTS, TEXT_FIELD, TOLERANCE, EXTEND, MIN_AREA, ONLY_NUMBERED, OUTPUT, PROBLEMS = (
        "INPUT", "TEXTS", "TEXT_FIELD", "TOLERANCE", "EXTEND", "MIN_AREA", "ONLY_NUMBERED", "OUTPUT", "PROBLEMS")

    def flags(self):
        return super().flags() | Qgis.ProcessingAlgorithmFlag.NoThreading

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterMultipleLayers(
            self.INPUT, tr("Lines of the plan (lines, polylines or polygons)"),
            Qgis.ProcessingSourceType.VectorAnyGeometry))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.TEXTS, tr("Plot numbers (texts as points)"), [Qgis.ProcessingSourceType.VectorPoint],
            optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.TEXT_FIELD, tr("Field with the text"), parentLayerParameterName=self.TEXTS, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.TOLERANCE, tr("Join ends that are closer than (map units)"),
            Qgis.ProcessingNumberParameterType.Double, 0.5, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.EXTEND, tr("Extend open lines along their direction up to (map units)"),
            Qgis.ProcessingNumberParameterType.Double, 2.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_AREA, tr("Ignore areas smaller than (map units²)"),
            Qgis.ProcessingNumberParameterType.Double, 1.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ONLY_NUMBERED, tr("Keep only areas with a plot number (drops streets and open spaces)"), False))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Plots"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.PROBLEMS, tr("Problems"), Qgis.ProcessingSourceType.VectorPoint))

    # ---- reading ----------------------------------------------------------------------------------
    @staticmethod
    def _lines(layers, crs, context, feedback):
        out = []
        for layer in layers:
            xform = None
            if layer.crs().isValid() and crs.isValid() and layer.crs() != crs:
                xform = QgsCoordinateTransform(layer.crs(), crs, context.transformContext())
            for f in layer.getFeatures():
                if feedback.isCanceled():
                    return out
                g = f.geometry()
                if g is None or g.isNull() or g.isEmpty():
                    continue
                g = QgsGeometry(g)
                if QgsWkbTypes.isCurvedType(g.wkbType()):
                    g = QgsGeometry(g.constGet().segmentize())
                if xform is not None:
                    try:
                        g.transform(xform)
                    except Exception:  # noqa: BLE001
                        continue
                if g.type() == Qgis.GeometryType.Polygon:
                    g = QgsGeometry(g.constGet().boundary())
                elif g.type() != Qgis.GeometryType.Line:
                    continue
                for part in g.constParts():
                    p = part.clone()
                    p.dropZValue()
                    p.dropMValue()
                    pg = QgsGeometry(p)
                    pl = pg.asPolyline()
                    if len(pl) >= 2 and pg.length() > 0:
                        out.append(pl)
        return out

    @staticmethod
    def _snap_ends(lines, tol):
        """Ends closer than `tol` become one point (their mean)."""
        if tol <= 0:
            return lines
        ends = []
        for i, pl in enumerate(lines):
            if pl[0] == pl[-1]:
                continue
            ends.append((i, 0, pl[0]))
            ends.append((i, -1, pl[-1]))
        parent = list(range(len(ends)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        grid = {}
        for k, (_, _, p) in enumerate(ends):
            grid.setdefault((math.floor(p.x() / tol), math.floor(p.y() / tol)), []).append(k)
        tol2 = tol * tol
        for (cx, cy), members in grid.items():
            near = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    near.extend(grid.get((cx + dx, cy + dy), ()))
            for a in members:
                pa = ends[a][2]
                for b in near:
                    if b <= a:
                        continue
                    pb = ends[b][2]
                    if (pa.x() - pb.x()) ** 2 + (pa.y() - pb.y()) ** 2 <= tol2:
                        ra, rb = find(a), find(b)
                        if ra != rb:
                            parent[rb] = ra
        clusters = {}
        for k in range(len(ends)):
            clusters.setdefault(find(k), []).append(k)
        lines = [list(pl) for pl in lines]
        for members in clusters.values():
            if len(members) < 2:
                continue
            # where most ends already meet is the drafter's corner; otherwise their mean
            spots = {}
            for k in members:
                spots.setdefault(_key(ends[k][2]), []).append(ends[k][2])
            ranked = sorted(spots.values(), key=len, reverse=True)
            if len(ranked) > 1 and len(ranked[0]) > len(ranked[1]):
                x, y = ranked[0][0].x(), ranked[0][0].y()
            else:
                x = sum(ends[k][2].x() for k in members) / len(members)
                y = sum(ends[k][2].y() for k in members) / len(members)
            for k in members:
                i, which, _ = ends[k]
                lines[i][which] = QgsPointXY(x, y)
        return lines

    @staticmethod
    def _edges(geom):
        out = []
        for part in geom.constParts():
            pl = QgsGeometry(part.clone()).asPolyline()
            if len(pl) >= 2:
                out.append(pl)
        return out

    # ---- the work -----------------------------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        layers = self.parameterAsLayerList(parameters, self.INPUT, context)
        if not layers:
            raise QgsProcessingException(tr("Choose the layers with the lines of the plan."))
        texts = self.parameterAsSource(parameters, self.TEXTS, context)
        text_field = self.parameterAsString(parameters, self.TEXT_FIELD, context)
        tol = self.parameterAsDouble(parameters, self.TOLERANCE, context)
        extend = self.parameterAsDouble(parameters, self.EXTEND, context)
        min_area = self.parameterAsDouble(parameters, self.MIN_AREA, context)
        only_numbered = self.parameterAsBool(parameters, self.ONLY_NUMBERED, context)
        crs = layers[0].crs()
        eps = max(tol * 0.01, 1e-4)

        feedback.pushInfo(tr("Reading the lines…"))
        lines = self._lines(layers, crs, context, feedback)
        if not lines:
            raise QgsProcessingException(tr("The chosen layers have no lines."))
        feedback.pushInfo(tr("{n} lines read.").format(n=len(lines)))
        lines = self._snap_ends(lines, tol)
        feedback.setProgress(15)

        # node the linework: duplicates and overlaps merge, crossings split
        noded = QgsGeometry.unaryUnion([QgsGeometry.fromPolylineXY(pl) for pl in lines if len(pl) >= 2])
        edges = self._edges(noded)
        feedback.setProgress(35)
        if feedback.isCanceled():
            return {}

        degree = {}
        for pl in edges:
            for p in (pl[0], pl[-1]):
                degree[_key(p)] = degree.get(_key(p), 0) + 1
        index = QgsSpatialIndex()
        edge_geoms = []
        for i, pl in enumerate(edges):
            g = QgsGeometry.fromPolylineXY(pl)
            edge_geoms.append(g)
            index.addFeature(i, g.boundingBox())

        connectors, problems = [], []
        reach = max(tol, extend)
        for i, pl in enumerate(edges):
            for end, prev, other in ((pl[-1], pl[-2], pl[0]), (pl[0], pl[1], pl[-1])):
                if degree.get(_key(end), 0) != 1:
                    continue
                if feedback.isCanceled():
                    return {}
                length = edge_geoms[i].length()
                if length <= max(tol, extend, eps) and degree.get(_key(other), 0) >= 3:
                    continue  # an overshoot past a line: harmless, dropped when the plots are built
                fix, gap = self._close(end, prev, other, i, edge_geoms, index, tol, extend, reach, eps)
                if fix is not None:
                    connectors.append(fix)
                else:
                    problems.append((end, gap))
        feedback.pushInfo(tr("{c} gaps closed, {p} open ends left.").format(c=len(connectors), p=len(problems)))
        feedback.setProgress(50)

        if connectors:
            noded = QgsGeometry.unaryUnion([noded] + [QgsGeometry.fromPolylineXY(c) for c in connectors])
        faces = QgsGeometry.polygonize([noded])
        polys = []
        slivers = 0
        for part in faces.constParts():
            g = QgsGeometry(part.clone())
            if g.area() < max(min_area, 1e-9):
                continue
            if tol > 0 and 2 * g.area() / max(g.length(), 1e-12) < tol:
                slivers += 1  # thinner on average than the gap tolerance: the space between two copies of a side
                continue
            polys.append(g)
        if slivers:
            feedback.pushInfo(tr("{n} thin slivers between doubled lines were ignored.").format(n=slivers))
        feedback.setProgress(65)

        # open spaces around blocks: big areas with holes that other plots fill
        if polys:
            areas = sorted(g.area() for g in polys)
            median = areas[len(areas) // 2]
            polys = [g for g in polys
                     if not (g.constGet().numInteriorRings() > 0 and g.area() > 3 * median and len(polys) > 2)]

        numbers = self._numbers(polys, texts, text_field, crs, context)
        da = QgsDistanceArea()
        da.setSourceCrs(crs, context.transformContext())
        if crs.isGeographic():
            da.setEllipsoid(crs.ellipsoidAcronym() or "EPSG:7030")
        else:
            da.setEllipsoid("NONE")

        fields = QgsFields()
        for name, typ in (("plot_no", QMetaType.Type.QString), ("label", QMetaType.Type.QString),
                          ("area_m2", QMetaType.Type.Double), ("perimeter_m", QMetaType.Type.Double),
                          ("x", QMetaType.Type.Double), ("y", QMetaType.Type.Double),
                          ("n_sides", QMetaType.Type.Int), ("sides", QMetaType.Type.QString),
                          ("note", QMetaType.Type.QString)):
            fields.append(QgsField(name, typ))
        sink, sink_id = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.Polygon, crs)
        pfields = QgsFields()
        pfields.append(QgsField("kind", QMetaType.Type.QString))
        pfields.append(QgsField("gap", QMetaType.Type.Double))
        pfields.append(QgsField("detail", QMetaType.Type.QString))
        psink, psink_id = self.parameterAsSink(parameters, self.PROBLEMS, context, pfields, Qgis.WkbType.Point, crs)

        def measure(a, b):
            return da.measureLine(a, b)

        written = numbered = 0
        for n, g in enumerate(polys):
            if feedback.isCanceled():
                return {}
            label, others = numbers[n]
            if only_numbered and texts is not None and not label:
                continue
            note = []
            if texts is not None and not label:
                note.append(tr("no plot number inside"))
                self._problem(psink, pfields, g.pointOnSurface().asPoint(), "no_number", None,
                              tr("This area has no plot number inside."))
            if others:
                note.append(tr("several numbers: {v}").format(v=", ".join([label] + others)))
                self._problem(psink, pfields, g.pointOnSurface().asPoint(), "several_numbers", None,
                              tr("Several plot numbers inside: {v}").format(v=", ".join([label] + others)))
            sides = side_lengths(g.constGet().exteriorRing().points(), measure)
            c = g.centroid().asPoint()
            f = QgsFeature(fields)
            f.setGeometry(g)
            f.setAttributes([normalise_digits(label) or None, label or None, round(da.measureArea(g), 3),
                             round(da.measurePerimeter(g), 3), round(c.x(), 4), round(c.y(), 4), len(sides),
                             " - ".join(f"{s:.2f}" for s in sides), "; ".join(note) or None])
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
            written += 1
            numbered += bool(label)
            feedback.setProgress(65 + 30 * n / max(len(polys), 1))

        for p, gap in problems:
            detail = (tr("Open end: the nearest line is {d} away.").format(d=f"{gap:.2f}") if gap is not None
                      else tr("Open end: no other line nearby."))
            self._problem(psink, pfields, p, "open_end", gap, detail)
        feedback.setProgress(100)
        feedback.pushInfo(tr("{n} plots built ({k} with a number); {p} open ends to check.").format(
            n=written, k=numbered, p=len(problems)))
        return {self.OUTPUT: sink_id, self.PROBLEMS: psink_id, "PLOT_COUNT": written, "NUMBERED_COUNT": numbered,
                "OPEN_ENDS": len(problems), "GAPS_CLOSED": len(connectors)}

    @staticmethod
    def _close(end, prev, other, own, edge_geoms, index, tol, extend, reach, eps):
        """A short connector line from a free end to the line it was meant to reach, or (None, gap)."""
        pt = QgsGeometry.fromPointXY(end)
        rect = QgsRectangle(end.x() - reach, end.y() - reach, end.x() + reach, end.y() + reach)
        best, best_d = None, None
        for j in index.intersects(rect):
            if j == own:
                continue
            g = edge_geoms[j]
            q = g.nearestPoint(pt).asPoint()
            if q.distance(other) < eps and g.distance(QgsGeometry.fromPointXY(other)) < eps:
                continue  # the line this end continues from
            d = q.distance(end)
            if d <= reach and (best_d is None or d < best_d):  # a long line's box can be near while it is far
                best, best_d = q, d
        if best is not None and best_d <= tol:
            if best_d < eps:  # touching but not noded: cross the line a little along the line's own direction
                dx, dy = end.x() - prev.x(), end.y() - prev.y()
            else:
                dx, dy = best.x() - end.x(), best.y() - end.y()
            norm = math.hypot(dx, dy) or 1.0
            tip = QgsPointXY(best.x() + dx / norm * eps, best.y() + dy / norm * eps)
            return [end, tip], best_d
        if extend > 0:
            dx, dy = end.x() - prev.x(), end.y() - prev.y()
            norm = math.hypot(dx, dy)
            if norm > 0:
                dx, dy = dx / norm, dy / norm
                ray = QgsGeometry.fromPolylineXY([end, QgsPointXY(end.x() + dx * extend, end.y() + dy * extend)])
                hit, hit_d = None, None
                for j in index.intersects(ray.boundingBox()):
                    if j == own:
                        continue
                    inter = ray.intersection(edge_geoms[j])
                    if inter is None or inter.isEmpty():
                        continue
                    for v in inter.vertices():
                        q = QgsPointXY(v.x(), v.y())
                        d = q.distance(end)
                        if d > eps and (hit_d is None or d < hit_d):
                            hit, hit_d = q, d
                if hit is not None:
                    return [end, QgsPointXY(hit.x() + dx * eps, hit.y() + dy * eps)], hit_d
        if best_d is None:  # how far is the nearest line? (for the report only)
            far = max(reach * 10, 10.0)
            for j in index.intersects(QgsRectangle(end.x() - far, end.y() - far, end.x() + far, end.y() + far)):
                if j == own:
                    continue
                g = edge_geoms[j]
                q = g.nearestPoint(pt).asPoint()
                if q.distance(other) < eps and g.distance(QgsGeometry.fromPointXY(other)) < eps:
                    continue
                d = q.distance(end)
                if d <= far and (best_d is None or d < best_d):
                    best_d = d
        return None, best_d

    @staticmethod
    def _problem(sink, fields, point, kind, gap, detail):
        f = QgsFeature(fields)
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(point)))
        f.setAttributes([kind, None if gap is None else round(gap, 3), detail])
        sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)

    @staticmethod
    def _numbers(polys, texts, text_field, crs, context):
        """For each plot: (number text, [other numbers inside])."""
        result = [("", [])] * len(polys)
        if texts is None:
            return result
        names = texts.fields().names()
        if not text_field:
            low = {n.lower(): n for n in names}
            text_field = next((low[k] for k in _NUMBER_FIELDS if k in low), None)
            if text_field is None:
                text_field = next((f.name() for f in texts.fields() if f.type() == QMetaType.Type.QString), None)
        if text_field is None or text_field not in names:
            return result
        xform = None
        if texts.sourceCrs().isValid() and crs.isValid() and texts.sourceCrs() != crs:
            xform = QgsCoordinateTransform(texts.sourceCrs(), crs, context.transformContext())
        index = QgsSpatialIndex()
        points = {}
        for f in texts.getFeatures():
            g = f.geometry()
            value = f[text_field]
            if g is None or g.isEmpty() or value is None or str(value).strip() == "":
                continue
            g = QgsGeometry(g)
            if xform is not None:
                try:
                    g.transform(xform)
                except Exception:  # noqa: BLE001
                    continue
            p = g.centroid().asPoint() if g.isMultipart() else g.asPoint()
            points[f.id()] = (QgsPointXY(p), str(value).strip())
            index.addFeature(f.id(), QgsRectangle(p.x(), p.y(), p.x(), p.y()))
        result = []
        for g in polys:
            engine = QgsGeometry.createGeometryEngine(g.constGet())
            engine.prepareGeometry()
            inside = []
            for fid in index.intersects(g.boundingBox()):
                p, value = points[fid]
                pg = QgsGeometry.fromPointXY(p)  # kept alive while the engine reads it
                if engine.contains(pg.constGet()):
                    inside.append((p, value))
            cands = [(p, v) for p, v in inside if v in number_candidates([v])]
            if not cands:
                result.append(("", []))
                continue
            centre = g.poleOfInaccessibility(max(g.boundingBox().width(), g.boundingBox().height()) / 100)[0]
            c = centre.asPoint()
            cands.sort(key=lambda pv: pv[0].distance(c))
            first = cands[0][1]
            others = []
            for _, v in cands[1:]:
                if normalise_digits(v) != normalise_digits(first) and v not in others:
                    others.append(v)
            result.append((first, others))
        return result
