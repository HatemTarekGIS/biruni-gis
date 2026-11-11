"""Building setbacks and coverage: checks building footprints against the parcels
they stand on - front/side/rear setbacks, buildings over the parcel line,
coverage, floor area ratio and number of floors - with the offending area of
each violation as a polygon.

Sides: the parcel outline is split at its corners. A side is a *front* when it
runs along a street (most of it within the front distance of a street line or
polygon), a *rear* when it is parallel to a front and touches none, otherwise a
*side*. Without streets every side uses the side setback."""
import math

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink,
                       QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessingException,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterFile, QgsProcessingParameterNumber,
                       QgsSpatialIndex)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from ..smart_table import parse_number, read_rows, split_header
from .base import BiruniAlgorithm, polygonal
from .quantities import norm

AREA_TOL = 0.01      # m²: smaller offending areas are drawing noise
DIST_TOL = 0.01      # m
CORNER_DEG = 5.0     # a change of direction below this is not a corner
PARALLEL_DEG = 30.0  # rear sides are this close to parallel to a front

RULE_HEADERS = {
    "use": ("use", "zoning", "zone", "code", "land use", "landuse", "category", "الاستخدام", "استخدام",
            "الاستعمال", "استعمال", "النشاط", "التصنيف", "الكود", "كود", "المنطقه"),
    "front": ("front", "front setback", "الامامي", "امامي", "ارتداد امامي", "الارتداد الامامي", "الواجهه"),
    "side": ("side", "side setback", "sides", "الجانبي", "جانبي", "ارتداد جانبي", "الارتداد الجانبي"),
    "rear": ("rear", "back", "rear setback", "الخلفي", "خلفي", "ارتداد خلفي", "الارتداد الخلفي"),
    "coverage": ("coverage", "coverage %", "coverage%", "max coverage", "site coverage", "نسبه البناء",
                 "نسبه التغطيه", "التغطيه", "نسبه البناء %", "البناء %"),
    "max_floors": ("max_floors", "max floors", "floors", "storeys", "stories", "الادوار", "عدد الادوار",
                   "الحد الاقصي للادوار", "الارتفاع"),
    "far": ("far", "floor area ratio", "max far", "معامل البناء", "معامل الاستغلال"),
}


def read_rules(path):
    """{normalised use value: {front, side, rear, coverage, max_floors, far}} from Excel/CSV."""
    headers, data = split_header(read_rows(path))
    col = {}
    for i, h in enumerate(headers):
        key = norm(h)
        for name, spellings in RULE_HEADERS.items():
            if name not in col and key in spellings:
                col[name] = i
    if "use" not in col:
        raise QgsProcessingException(tr("The rules table needs a column with the use or zoning code "
                                        "(for example: Use, Front, Side, Rear, Coverage, Max floors, FAR)."))
    rules = {}
    for row in data:
        key = norm(row[col["use"]] if col["use"] < len(row) else None)
        if not key:
            continue
        rule = {name: parse_number(row[i]) if i < len(row) else None for name, i in col.items() if name != "use"}
        if rule.get("coverage") is not None and 0 < rule["coverage"] <= 1:
            rule["coverage"] *= 100          # 0.6 written as a fraction
        rules[key] = rule
    return rules


def _angle(p, q):
    return math.degrees(math.atan2(q.y() - p.y(), q.x() - p.x()))


def _turn(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def parcel_sides(geom):
    """The sides of a parcel: [[QgsPointXY…]] per side, split at the corners of each exterior ring."""
    if geom.constGet().hasCurvedSegments():
        geom = QgsGeometry(geom.constGet().segmentize())
    sides = []
    polys = geom.asMultiPolygon() if geom.isMultipart() else [geom.asPolygon()]
    for poly in polys:
        if not poly:
            continue
        pts = []
        for p in poly[0][:-1]:
            if not pts or p.sqrDist(pts[-1]) > 1e-12:
                pts.append(p)
        if len(pts) > 1 and pts[0].sqrDist(pts[-1]) <= 1e-12:
            pts.pop()
        n = len(pts)
        if n < 3:
            continue
        heading = [_angle(pts[i], pts[(i + 1) % n]) for i in range(n)]
        corners = [i for i in range(n) if _turn(heading[i - 1], heading[i]) >= CORNER_DEG]
        if not corners:
            sides.append(pts + [pts[0]])
            continue
        for k, c in enumerate(corners):
            end = corners[(k + 1) % len(corners)]
            run, i = [pts[c]], c
            while True:
                i = (i + 1) % n
                run.append(pts[i])
                if i == end:
                    break
            sides.append(run)
    return sides


class _Streets:
    """Distances to streets (lines or polygons) through a spatial index."""

    def __init__(self, geoms):
        self.geoms = geoms
        self.index = QgsSpatialIndex()
        for i, g in geoms.items():
            self.index.addFeature(i, g.boundingBox())

    def distance(self, point):
        pg = QgsGeometry.fromPointXY(point)
        near = self.index.nearestNeighbor(point, 4)
        return min((self.geoms[i].distance(pg) for i in near), default=math.inf)


def classify_sides(sides, streets, front_dist):
    """'front' / 'side' / 'rear' for each side."""
    kinds = ["side"] * len(sides)
    if streets is None or not streets.geoms:
        return kinds
    for k, side in enumerate(sides):
        line = QgsGeometry.fromPolylineXY(side)
        length = line.length()
        samples = [line.interpolate(length * t / 10).asPoint() for t in range(11)] if length else side
        d = sorted(streets.distance(QgsPointXY(p)) for p in samples)
        if d[int(0.8 * (len(d) - 1))] <= front_dist + DIST_TOL:   # most of the side runs along a street
            kinds[k] = "front"
    fronts = [k for k, kind in enumerate(kinds) if kind == "front"]
    for k, side in enumerate(sides):
        if kinds[k] != "side" or not fronts:
            continue
        a = _angle(side[0], side[-1])
        touches = any(min(p.sqrDist(q) for p in (side[0], side[-1]) for q in (sides[f][0], sides[f][-1])) <= 1e-8
                      for f in fronts)
        parallel = any(min(_turn(a, _angle(sides[f][0], sides[f][-1])),
                           _turn(a + 180, _angle(sides[f][0], sides[f][-1]))) <= PARALLEL_DEG for f in fronts)
        if parallel and not touches:
            kinds[k] = "rear"
    return kinds


def _utm_for(extent, crs, transform_context):
    to_wgs = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"), transform_context)
    c = to_wgs.transform(extent.center())
    zone = min(60, max(1, int((c.x() + 180) // 6) + 1))
    return QgsCoordinateReferenceSystem(f"EPSG:{(32600 if c.y() >= 0 else 32700) + zone}")


class SetbackCheck(BiruniAlgorithm):
    ID = "setbackcheck"
    NAME = "Building setbacks and coverage check"
    GROUP = "Engineering office"
    GROUP_ID = "office"
    HELP = ("Checks building footprints against their parcels: the front, side and rear setbacks (the distance "
            "from each parcel side), buildings that cross the parcel line or stand on no parcel, the building "
            "coverage %, the floor area ratio and the number of floors. Front sides are the parcel sides that run "
            "along a street (street centre lines, edges or street polygons); the side opposite a front is the "
            "rear. Rules can differ per use or zoning code from an Excel/CSV table (Use, Front, Side, Rear, "
            "Coverage, Max floors, FAR). Every violation is drawn as the offending part of the building with the "
            "required and actual values; the parcels get a compliant / non-compliant status.")

    (PARCELS, BUILDINGS, STREETS, FRONT_DIST, FRONT, SIDE, REAR, COVERAGE, MAX_FLOORS, MAX_FAR, FLOORS, USE,
     RULES, VIOLATIONS, SUMMARY) = (
        "PARCELS", "BUILDINGS", "STREETS", "FRONT_DISTANCE", "FRONT", "SIDE", "REAR", "COVERAGE", "MAX_FLOORS",
        "MAX_FAR", "FLOORS_FIELD", "USE_FIELD", "RULES", "VIOLATIONS", "SUMMARY")

    def initAlgorithm(self, config=None):  # noqa: N802
        poly = [Qgis.ProcessingSourceType.VectorPolygon]
        self.addParameter(QgsProcessingParameterFeatureSource(self.PARCELS, tr("Parcels"), poly))
        self.addParameter(QgsProcessingParameterFeatureSource(self.BUILDINGS, tr("Buildings (footprints)"), poly))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.STREETS, tr("Streets (lines or polygons) - optional, to find the front sides"),
            [Qgis.ProcessingSourceType.VectorLine, Qgis.ProcessingSourceType.VectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.FRONT_DIST, tr("A side is a front when it runs within this distance of a street (m)"),
            Qgis.ProcessingNumberParameterType.Double, 10.0, minValue=0.0))
        for name, label, value in ((self.FRONT, tr("Front setback (m)"), 3.0),
                                   (self.SIDE, tr("Side setback (m)"), 2.0),
                                   (self.REAR, tr("Rear setback (m)"), 2.0)):
            self.addParameter(QgsProcessingParameterNumber(
                name, label, Qgis.ProcessingNumberParameterType.Double, value, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.COVERAGE, tr("Maximum building coverage % (0 = no check)"),
            Qgis.ProcessingNumberParameterType.Double, 60.0, minValue=0.0, maxValue=100.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_FLOORS, tr("Maximum floors (0 = no check)"), Qgis.ProcessingNumberParameterType.Integer, 0,
            minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_FAR, tr("Maximum floor area ratio (0 = no check)"), Qgis.ProcessingNumberParameterType.Double,
            0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterField(
            self.FLOORS, tr("Number of floors field (buildings) - optional"), parentLayerParameterName=self.BUILDINGS,
            type=Qgis.ProcessingFieldParameterDataType.Numeric, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.USE, tr("Use / zoning field (parcels) - for the rules table"), parentLayerParameterName=self.PARCELS,
            optional=True))
        self.addParameter(QgsProcessingParameterFile(
            self.RULES, tr("Rules table per use (Excel or CSV) - optional"), optional=True,
            fileFilter="Excel / CSV (*.xlsx *.xlsm *.xls *.ods *.csv *.txt);;All files (*.*)"))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.VIOLATIONS, tr("Violations"), Qgis.ProcessingSourceType.VectorPolygon))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.SUMMARY, tr("Parcels status"), Qgis.ProcessingSourceType.VectorPolygon))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        parcels = self.parameterAsSource(parameters, self.PARCELS, context)
        buildings = self.parameterAsSource(parameters, self.BUILDINGS, context)
        if parcels is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.PARCELS))
        if buildings is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.BUILDINGS))
        streets_src = self.parameterAsSource(parameters, self.STREETS, context)
        front_dist = self.parameterAsDouble(parameters, self.FRONT_DIST, context)
        default = {"front": self.parameterAsDouble(parameters, self.FRONT, context),
                   "side": self.parameterAsDouble(parameters, self.SIDE, context),
                   "rear": self.parameterAsDouble(parameters, self.REAR, context),
                   "coverage": self.parameterAsDouble(parameters, self.COVERAGE, context),
                   "max_floors": self.parameterAsInt(parameters, self.MAX_FLOORS, context),
                   "far": self.parameterAsDouble(parameters, self.MAX_FAR, context)}
        floors_name = self.parameterAsString(parameters, self.FLOORS, context)
        use_name = self.parameterAsString(parameters, self.USE, context)
        rules_path = self.parameterAsFile(parameters, self.RULES, context)
        rules = read_rules(rules_path) if rules_path else {}
        if rules and not use_name:
            raise QgsProcessingException(tr("A rules table needs the use / zoning field of the parcels."))

        # Work in metres: geographic layers are measured in their UTM zone
        out_crs = parcels.sourceCrs()
        work = out_crs
        if not work.isValid() or work.isGeographic():
            work = _utm_for(parcels.sourceExtent(), out_crs, context.transformContext())
            feedback.pushInfo(tr("Measuring in {crs} (metres).").format(crs=work.authid()))
        tc = context.transformContext()

        def to_work(src):
            if src.sourceCrs() == work or not src.sourceCrs().isValid():
                return None
            return QgsCoordinateTransform(src.sourceCrs(), work, tc)

        back = QgsCoordinateTransform(work, out_crs, tc) if work != out_crs else None

        def load(src, xf):
            for f in src.getFeatures():
                g = f.geometry()
                if g.isNull() or g.isEmpty():
                    continue
                g = QgsGeometry(g)
                if xf:
                    g.transform(xf)
                if not g.isGeosValid():
                    g = g.makeValid()
                yield f, g

        pid_use, parcel_geom, parcel_attrs = {}, {}, {}
        pindex = QgsSpatialIndex()
        i_use = parcels.fields().lookupField(use_name) if use_name else -1
        for f, g in load(parcels, to_work(parcels)):
            g = polygonal(g)
            if g is None:
                continue
            parcel_geom[f.id()] = g
            parcel_attrs[f.id()] = f.attributes()
            pid_use[f.id()] = f.attributes()[i_use] if i_use >= 0 else None
            pindex.addFeature(f.id(), g.boundingBox())
        feedback.setProgress(10)

        streets = None
        if streets_src is not None:
            sg = {f.id(): g for f, g in load(streets_src, to_work(streets_src))}
            streets = _Streets(sg)
        i_floors = buildings.fields().lookupField(floors_name) if floors_name else -1
        bgeom, bfloors = {}, {}
        for f, g in load(buildings, to_work(buildings)):
            g = polygonal(g)
            if g is None:
                continue
            bgeom[f.id()] = g
            fl = parse_number(f.attributes()[i_floors]) if i_floors >= 0 else None
            bfloors[f.id()] = fl if fl is not None and fl > 0 else 1.0
        feedback.setProgress(20)

        # Each building belongs to the parcel it overlaps most
        owner = {}
        for bid, g in bgeom.items():
            best, area = None, AREA_TOL
            for pid in pindex.intersects(g.boundingBox()):
                inter = polygonal(g.intersection(parcel_geom[pid]))
                a = inter.area() if inter else 0.0
                if a > area:
                    best, area = pid, a
            owner[bid] = best
        on_parcel = {}
        for bid, pid in owner.items():
            if pid is not None:
                on_parcel.setdefault(pid, []).append(bid)

        vf = QgsFields()
        for name, typ in (("parcel_id", QMetaType.Type.LongLong), ("building_id", QMetaType.Type.LongLong),
                          ("rule", QMetaType.Type.QString), ("side_no", QMetaType.Type.Int),
                          ("side_type", QMetaType.Type.QString), ("required", QMetaType.Type.Double),
                          ("actual", QMetaType.Type.Double), ("shortfall", QMetaType.Type.Double),
                          ("area_m2", QMetaType.Type.Double)):
            vf.append(QgsField(name, typ))
        sf = QgsFields()
        for fld in parcels.fields():
            sf.append(fld)
        extra = [("parcel_id", QMetaType.Type.LongLong), ("rule_set", QMetaType.Type.QString),
                 ("buildings", QMetaType.Type.Int), ("parcel_m2", QMetaType.Type.Double),
                 ("built_m2", QMetaType.Type.Double), ("coverage_pct", QMetaType.Type.Double),
                 ("far", QMetaType.Type.Double), ("violations", QMetaType.Type.Int), ("status", QMetaType.Type.QString)]
        taken = {f.name().lower() for f in parcels.fields()}
        extra_names = []
        for name, typ in extra:
            n, k = name, 2
            while n.lower() in taken:
                n, k = f"{name}_{k}", k + 1
            taken.add(n.lower())
            extra_names.append(n)
            sf.append(QgsField(n, typ))
        vsink, vid = self.parameterAsSink(parameters, self.VIOLATIONS, context, vf, Qgis.WkbType.MultiPolygon,
                                          out_crs)
        ssink, sid = self.parameterAsSink(parameters, self.SUMMARY, context, sf, Qgis.WkbType.MultiPolygon, out_crs)

        kinds_label = {"front": tr("Front"), "side": tr("Lateral"), "rear": tr("Rear")}
        count = {"v": 0}

        def violation(geom, pid, bid, rule, required=None, actual=None, shortfall=None, side_no=None, side=""):
            g = polygonal(geom) if geom is not None else None
            area = g.area() if g else 0.0
            if g is not None and back:
                g.transform(back)
            feat = QgsFeature(vf)
            if g is not None:
                feat.setGeometry(g)
            rnd = (lambda v: None if v is None else round(v, 3))
            feat.setAttributes([pid, bid, rule, side_no, side, rnd(required), rnd(actual), rnd(shortfall),
                                round(area, 3)])
            vsink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
            count["v"] += 1

        # Buildings standing on no parcel
        for bid, pid in owner.items():
            if pid is None:
                violation(bgeom[bid], None, bid, tr("No parcel"))

        missing_rules = set()
        noncompliant = 0
        total = len(parcel_geom) or 1
        for n, (pid, pg) in enumerate(parcel_geom.items()):
            if feedback.isCanceled():
                return {}
            rule = dict(default)
            rule_set = tr("Default")
            if rules:
                key = norm(pid_use[pid])
                if key in rules:
                    rule.update({k: v for k, v in rules[key].items() if v is not None})
                    rule_set = str(pid_use[pid])
                else:
                    missing_rules.add("-" if pid_use[pid] is None else str(pid_use[pid]))
            before = count["v"]
            parea = pg.area()
            bids = on_parcel.get(pid, [])
            sides = parcel_sides(pg)
            kinds = classify_sides(sides, streets, front_dist)
            side_lines = [QgsGeometry.fromPolylineXY(s) for s in sides]
            inside_parts = []
            floor_area = 0.0
            for bid in bids:
                bg = bgeom[bid]
                outside = polygonal(bg.difference(pg))
                if outside is not None and outside.area() > AREA_TOL:
                    boundary = QgsGeometry(pg.constGet().boundary())
                    depth = max((boundary.distance(QgsGeometry.fromPointXY(QgsPointXY(v))) for v in outside.vertices()),
                                default=0.0)
                    violation(outside, pid, bid, tr("Outside the parcel"), 0.0, None, depth)
                inside = polygonal(bg.intersection(pg))
                if inside is None:
                    continue
                inside_parts.append(inside)
                floor_area += inside.area() * bfloors[bid]
                for k, (line, kind) in enumerate(zip(side_lines, kinds)):
                    req = rule.get(kind) or 0.0
                    if req <= 0:
                        continue
                    actual = inside.distance(line)
                    if req - actual <= DIST_TOL:
                        continue
                    band = line.buffer(req, 8, Qgis.EndCapStyle.Flat, Qgis.JoinStyle.Round, 2.0)
                    off = polygonal(inside.intersection(band))
                    if off is None or off.area() <= AREA_TOL:
                        continue
                    violation(off, pid, bid, tr("Setback"), req, actual, req - actual, k + 1, kinds_label[kind])
                max_floors = rule.get("max_floors") or 0
                if max_floors and bfloors[bid] > max_floors:
                    violation(bg, pid, bid, tr("Floors"), max_floors, bfloors[bid], bfloors[bid] - max_floors)
            built = QgsGeometry.unaryUnion(inside_parts) if inside_parts else None
            built_area = built.area() if built else 0.0
            cov = 100.0 * built_area / parea if parea else 0.0
            far = floor_area / parea if parea else 0.0
            max_cov = rule.get("coverage") or 0
            if max_cov and cov - max_cov > 1e-6:
                violation(built, pid, None, tr("Building coverage"), max_cov, cov, cov - max_cov)
            max_far = rule.get("far") or 0
            if max_far and far - max_far > 1e-6:
                violation(built, pid, None, tr("Floor area ratio"), max_far, far, far - max_far)
            nv = count["v"] - before
            if nv:
                noncompliant += 1
            status = tr("Non-compliant") if nv else (tr("Compliant") if bids else tr("No buildings"))
            g = QgsGeometry(pg)
            if back:
                g.transform(back)
            feat = QgsFeature(sf)
            feat.setGeometry(g)
            feat.setAttributes(list(parcel_attrs[pid]) + [pid, rule_set, len(bids), round(parea, 3),
                                                         round(built_area, 3), round(cov, 2), round(far, 3), nv,
                                                         status])
            ssink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
            feedback.setProgress(20 + 80 * (n + 1) / total)

        if missing_rules:
            feedback.reportError(tr("No rules for these uses, the default values were used: {u}").format(
                u=", ".join(sorted(missing_rules))))
        unowned = sum(1 for p in owner.values() if p is None)
        feedback.pushInfo(tr("{p} parcels checked: {n} non-compliant, {v} violations, {b} buildings on no "
                             "parcel.").format(p=len(parcel_geom), n=noncompliant, v=count["v"], b=unowned))
        return {self.VIOLATIONS: vid, self.SUMMARY: sid, "VIOLATION_COUNT": count["v"],
                "NONCOMPLIANT_COUNT": noncompliant, "PARCEL_COUNT": len(parcel_geom), "BUILDING_COUNT": len(bgeom),
                "NO_PARCEL_COUNT": unowned}
