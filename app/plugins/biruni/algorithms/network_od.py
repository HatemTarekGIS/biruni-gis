import math

from qgis.analysis import (QgsGraphAnalyzer, QgsGraphBuilder, QgsNetworkDistanceStrategy, QgsNetworkSpeedStrategy,
                           QgsVectorLayerDirector)
from qgis.core import (Qgis, QgsDistanceArea, QgsFeature, QgsFeatureRequest, QgsFeatureSink, QgsField, QgsFields,
                       QgsGeometry, QgsPointXY, QgsProcessingException, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterNumber, QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .base import BiruniAlgorithm

FORWARD = {"yes", "y", "true", "1", "ft", "f", "forward", "oneway", "one way", "one-way"}
BACKWARD = {"-1", "tf", "t", "backward", "reverse", "r", "back"}


def direction_code(value):
    """'F' (one way along the line), 'T' (against it) or 'B' (both ways)."""
    if value is None:
        return "B"
    v = str(value).strip().lower()
    if v in FORWARD:
        return "F"
    if v in BACKWARD:
        return "T"
    return "B"


class RoadNetwork:
    """A routable graph built from a road layer plus the points to route between."""

    def __init__(self, source, context, feedback, dir_field="", speed_field="", default_speed=50.0, tolerance=0.0):
        self.crs = source.sourceCrs()
        crs_id = self.crs.authid() or self.crs.toWkt()
        self.layer = QgsVectorLayer(f"MultiLineString?crs={crs_id}&field=dir:string&field=speed:double",
                                    "network", "memory")
        if not self.crs.authid():
            self.layer.setCrs(self.crs)
        names = source.fields().names()
        di = names.index(dir_field) if dir_field and dir_field in names else -1
        si = names.index(speed_field) if speed_field and speed_field in names else -1
        feats = []
        for f in source.getFeatures():
            if feedback.isCanceled():
                break
            g = f.geometry()
            if g.isNull() or g.isEmpty():
                continue
            g = QgsGeometry(g)
            g.convertToMultiType()
            speed = default_speed
            if si >= 0:
                try:
                    s = float(f.attributes()[si])
                    if s > 0 and not math.isnan(s):
                        speed = s
                except (TypeError, ValueError):
                    pass
            nf = QgsFeature(self.layer.fields())
            nf.setGeometry(g)
            nf.setAttributes([direction_code(f.attributes()[di]) if di >= 0 else "B", speed])
            feats.append(nf)
        self.layer.dataProvider().addFeatures(feats)
        if not feats:
            raise QgsProcessingException(tr("The road layer has no lines."))

        self.director = QgsVectorLayerDirector(self.layer, 0, "F", "T", "B",
                                               QgsVectorLayerDirector.Direction.DirectionBoth)
        self.director.addStrategy(QgsNetworkDistanceStrategy())
        self.director.addStrategy(QgsNetworkSpeedStrategy(1, default_speed, 1000.0 / 3600.0))
        self.ellipsoid = context.ellipsoid() if context.ellipsoid() and context.ellipsoid() != "NONE" \
            else (self.crs.ellipsoidAcronym() or "EPSG:7030")
        self.tolerance = tolerance
        self.da = QgsDistanceArea()
        self.da.setSourceCrs(self.crs, context.transformContext())
        self.da.setEllipsoid(self.ellipsoid)
        self.graph = None

    def build(self, points):
        """Add the points to the graph; returns their vertex indices."""
        builder = QgsGraphBuilder(self.crs, True, self.tolerance, self.ellipsoid)
        tied = self.director.makeGraph(builder, points)
        self.graph = builder.takeGraph()
        self.tied = tied
        return [self.graph.findVertex(p) for p in tied]

    def solve(self, vertex, criterion):
        return QgsGraphAnalyzer.dijkstra(self.graph, vertex, criterion)

    def path(self, tree, start, end, want_points=False):
        """(points or None, distance m, time s) along the tree, or None when unreachable."""
        if end == start:
            return ([self.graph.vertex(end).point()] * 2 if want_points else None), 0.0, 0.0
        if end < 0 or tree[end] == -1:
            return None
        dist = secs = 0.0
        pts = [self.graph.vertex(end).point()] if want_points else None
        v = end
        guard = 0
        while v != start:
            e = self.graph.edge(tree[v])
            dist += float(e.cost(0))
            secs += float(e.cost(1))
            v = e.fromVertex()
            if want_points:
                pts.append(self.graph.vertex(v).point())
            guard += 1
            if guard > self.graph.edgeCount() + 1 or tree[v] == -1 and v != start:
                return None
        if want_points:
            pts.reverse()
        return pts, dist, secs

    def straight(self, a, b):
        return self.da.measureLine(QgsPointXY(a), QgsPointXY(b))


def read_points(source, crs, context, id_field):
    """[(id value, QgsPointXY)] of a point source in ``crs``."""
    req = QgsFeatureRequest().setDestinationCrs(crs, context.transformContext())
    names = source.fields().names()
    idx = names.index(id_field) if id_field and id_field in names else -1
    out = []
    for f in source.getFeatures(req):
        g = f.geometry()
        if g.isNull() or g.isEmpty():
            continue
        p = g.asMultiPoint()[0] if g.isMultipart() else g.asPoint()
        out.append((f.attributes()[idx] if idx >= 0 else f.id(), QgsPointXY(p)))
    return out


def id_field(source, name, out_name):
    if name and name in source.fields().names():
        f = QgsField(source.fields().field(name))
        f.setName(out_name)
        return f
    return QgsField(out_name, QMetaType.Type.LongLong)


class _NetworkAlgorithm(BiruniAlgorithm):
    GROUP = "Networks"
    GROUP_ID = "networks"
    NETWORK, COST, DIR_FIELD, SPEED_FIELD, SPEED, TOLERANCE = (
        "NETWORK", "COST", "DIRECTION_FIELD", "SPEED_FIELD", "DEFAULT_SPEED", "TOLERANCE")

    def add_network_parameters(self):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.NETWORK, tr("Road network (lines)"), [Qgis.ProcessingSourceType.VectorLine]))
        self.addParameter(QgsProcessingParameterEnum(self.COST, tr("Route by"), [tr("Shortest (distance)"), tr("Fastest (time)")],
                                                     defaultValue=0))

    def add_road_options(self):
        self.addParameter(QgsProcessingParameterField(
            self.DIR_FIELD, tr("One-way field (yes / 1 / FT = along the line, -1 / TF = against it)"),
            parentLayerParameterName=self.NETWORK, optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.SPEED_FIELD, tr("Speed field (km/h)"), parentLayerParameterName=self.NETWORK,
            type=Qgis.ProcessingFieldParameterDataType.Numeric, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.SPEED, tr("Default speed (km/h)"), Qgis.ProcessingNumberParameterType.Double, 50.0, minValue=1.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.TOLERANCE, tr("Join line ends closer than (layer units)"),
            Qgis.ProcessingNumberParameterType.Double, 0.0, minValue=0.0))

    def network(self, parameters, context, feedback):
        source = self.parameterAsSource(parameters, self.NETWORK, context)
        if source is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.NETWORK))
        feedback.pushInfo(tr("Building the road network…"))
        return RoadNetwork(source, context, feedback,
                           self.parameterAsString(parameters, self.DIR_FIELD, context),
                           self.parameterAsString(parameters, self.SPEED_FIELD, context),
                           self.parameterAsDouble(parameters, self.SPEED, context),
                           self.parameterAsDouble(parameters, self.TOLERANCE, context))


class ClosestFacility(_NetworkAlgorithm):
    ID = "closestfacility"
    NAME = "Closest facility (roads)"
    HELP = ("Finds, for every incident (customer, accident, house …), the nearest facilities (hospitals, "
            "branches, schools …) by road - not as the crow flies - and draws the route to each. Choose "
            "shortest distance or fastest time (speed field or default speed); a one-way field is respected. "
            "Each route gets the network distance in metres, the travel time in minutes and the rank. "
            "Incidents that cannot reach any facility are listed with reachable = 0.")

    INCIDENTS, INCIDENT_ID, FACILITIES, FACILITY_ID, COUNT, TRAVEL, OUTPUT = (
        "INCIDENTS", "INCIDENT_ID", "FACILITIES", "FACILITY_ID", "COUNT", "TRAVEL", "OUTPUT")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.add_network_parameters()
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INCIDENTS, tr("Incidents (points)"), [Qgis.ProcessingSourceType.VectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            self.INCIDENT_ID, tr("Incident id field"), parentLayerParameterName=self.INCIDENTS, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.FACILITIES, tr("Facilities (points)"), [Qgis.ProcessingSourceType.VectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            self.FACILITY_ID, tr("Facility id field"), parentLayerParameterName=self.FACILITIES, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.COUNT, tr("Number of facilities to find"), Qgis.ProcessingNumberParameterType.Integer, 1,
            minValue=1))
        self.addParameter(QgsProcessingParameterEnum(
            self.TRAVEL, tr("Travel direction"), [tr("From incident to facility"), tr("From facility to incident")],
            defaultValue=0))
        self.add_road_options()
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, tr("Routes"), Qgis.ProcessingSourceType.VectorLine))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        net = self.network(parameters, context, feedback)
        inc_src = self.parameterAsSource(parameters, self.INCIDENTS, context)
        fac_src = self.parameterAsSource(parameters, self.FACILITIES, context)
        if inc_src is None or fac_src is None:
            raise QgsProcessingException(tr("Choose the incident and facility layers."))
        inc_field = self.parameterAsString(parameters, self.INCIDENT_ID, context)
        fac_field = self.parameterAsString(parameters, self.FACILITY_ID, context)
        incidents = read_points(inc_src, net.crs, context, inc_field)
        facilities = read_points(fac_src, net.crs, context, fac_field)
        n_best = self.parameterAsInt(parameters, self.COUNT, context)
        to_incident = self.parameterAsEnum(parameters, self.TRAVEL, context) == 1
        criterion = self.parameterAsEnum(parameters, self.COST, context)

        fields = QgsFields()
        fields.append(id_field(inc_src, inc_field, "incident_id"))
        fields.append(id_field(fac_src, fac_field, "facility_id"))
        for name, typ in (("rank", QMetaType.Type.Int), ("distance_m", QMetaType.Type.Double),
                          ("time_min", QMetaType.Type.Double), ("straight_m", QMetaType.Type.Double),
                          ("access_m", QMetaType.Type.Double), ("reachable", QMetaType.Type.Int)):
            fields.append(QgsField(name, typ))
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, Qgis.WkbType.LineString, net.crs)

        verts = net.build([p for _, p in incidents] + [p for _, p in facilities])
        iv, fv = verts[:len(incidents)], verts[len(incidents):]
        snap = [net.straight(p, t) for p, t in zip([p for _, p in incidents] + [p for _, p in facilities], net.tied)]

        # results[i] = [(cost, facility index, path)]
        results = [[] for _ in incidents]
        if to_incident:
            for j, v in enumerate(fv):
                if feedback.isCanceled():
                    return {}
                tree, costs = net.solve(v, criterion)
                for i, u in enumerate(iv):
                    if math.isfinite(costs[u]) and (u == v or tree[u] != -1):
                        results[i].append((costs[u], j, (tree, v, u)))
                feedback.setProgress(90 * (j + 1) / max(len(fv), 1))
        else:
            for i, u in enumerate(iv):
                if feedback.isCanceled():
                    return {}
                tree, costs = net.solve(u, criterion)
                for j, v in enumerate(fv):
                    if math.isfinite(costs[v]) and (u == v or tree[v] != -1):
                        results[i].append((costs[v], j, (tree, u, v)))
                feedback.setProgress(90 * (i + 1) / max(len(iv), 1))

        routes = unreachable = 0
        for i, (iid, ip) in enumerate(incidents):
            best = sorted(results[i], key=lambda r: r[0])[:n_best]
            if not best:
                f = QgsFeature(fields)
                f.setAttributes([iid, None, None, None, None, None, snap[i], 0])
                sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
                unreachable += 1
                continue
            for rank, (_cost, j, (tree, start, end)) in enumerate(best, 1):
                found = net.path(tree, start, end, want_points=True)
                if found is None:
                    continue
                pts, dist, secs = found
                fid, fp = facilities[j]
                f = QgsFeature(fields)
                f.setGeometry(QgsGeometry.fromPolylineXY(pts))
                f.setAttributes([iid, fid, rank, dist, secs / 60.0, net.straight(ip, fp),
                                 snap[i] + snap[len(incidents) + j], 1])
                sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
                routes += 1
        feedback.pushInfo(tr("{r} routes; {u} incidents cannot reach any facility.").format(r=routes, u=unreachable))
        return {self.OUTPUT: dest, "ROUTE_COUNT": routes, "UNREACHABLE_COUNT": unreachable}


class OdMatrix(_NetworkAlgorithm):
    ID = "odmatrix"
    NAME = "Origin-destination matrix (roads)"
    HELP = ("Measures the road distance and travel time between every origin and every destination - for "
            "delivery planning, service coverage or choosing a site. Leave the destinations empty to measure "
            "between the origins themselves. The output can be straight lines, the actual routes, or a plain "
            "table; every pair gets the network distance (m), the time (min) and the straight-line distance. "
            "Pairs that are not connected by the roads have reachable = 0.")

    ORIGINS, ORIGIN_ID, DESTS, DEST_ID, GEOMETRY, OUTPUT = (
        "ORIGINS", "ORIGIN_ID", "DESTINATIONS", "DESTINATION_ID", "GEOMETRY", "OUTPUT")

    def initAlgorithm(self, config=None):  # noqa: N802
        self.add_network_parameters()
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.ORIGINS, tr("Origins (points)"), [Qgis.ProcessingSourceType.VectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            self.ORIGIN_ID, tr("Origin id field"), parentLayerParameterName=self.ORIGINS, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.DESTS, tr("Destinations (points, empty = the origins)"), [Qgis.ProcessingSourceType.VectorPoint],
            optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.DEST_ID, tr("Destination id field"), parentLayerParameterName=self.DESTS, optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.GEOMETRY, tr("Output geometry"), [tr("Straight lines"), tr("Routes along the roads"),
                                    tr("No geometry (table)")], defaultValue=0))
        self.add_road_options()
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, tr("OD matrix")))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        net = self.network(parameters, context, feedback)
        o_src = self.parameterAsSource(parameters, self.ORIGINS, context)
        if o_src is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.ORIGINS))
        d_src = self.parameterAsSource(parameters, self.DESTS, context)
        o_field = self.parameterAsString(parameters, self.ORIGIN_ID, context)
        d_field = self.parameterAsString(parameters, self.DEST_ID, context) if d_src is not None else o_field
        origins = read_points(o_src, net.crs, context, o_field)
        dests = read_points(d_src, net.crs, context, d_field) if d_src is not None else origins
        mode = self.parameterAsEnum(parameters, self.GEOMETRY, context)
        criterion = self.parameterAsEnum(parameters, self.COST, context)

        fields = QgsFields()
        fields.append(id_field(o_src, o_field, "origin_id"))
        fields.append(id_field(d_src if d_src is not None else o_src, d_field, "destination_id"))
        for name, typ in (("distance_m", QMetaType.Type.Double), ("time_min", QMetaType.Type.Double),
                          ("straight_m", QMetaType.Type.Double), ("reachable", QMetaType.Type.Int)):
            fields.append(QgsField(name, typ))
        wkb = Qgis.WkbType.NoGeometry if mode == 2 else Qgis.WkbType.LineString
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, fields, wkb, net.crs)

        verts = net.build([p for _, p in origins] + ([p for _, p in dests] if d_src is not None else []))
        ov = verts[:len(origins)]
        dv = verts[len(origins):] if d_src is not None else ov
        pairs = unreachable = 0
        for i, u in enumerate(ov):
            if feedback.isCanceled():
                return {}
            tree, _costs = net.solve(u, criterion)
            oid, op = origins[i]
            for j, v in enumerate(dv):
                did, dp = dests[j]
                found = net.path(tree, u, v, want_points=mode == 1)
                f = QgsFeature(fields)
                if found is None:
                    f.setAttributes([oid, did, None, None, net.straight(op, dp), 0])
                    unreachable += 1
                else:
                    pts, dist, secs = found
                    f.setAttributes([oid, did, dist, secs / 60.0, net.straight(op, dp), 1])
                if mode == 0:
                    f.setGeometry(QgsGeometry.fromPolylineXY([op, dp]))
                elif mode == 1 and found is not None:
                    f.setGeometry(QgsGeometry.fromPolylineXY(pts))
                sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
                pairs += 1
            feedback.setProgress(100 * (i + 1) / max(len(ov), 1))
        feedback.pushInfo(tr("{n} pairs measured; {u} not connected by the roads.").format(n=pairs, u=unreachable))
        return {self.OUTPUT: dest, "PAIR_COUNT": pairs, "UNREACHABLE_COUNT": unreachable}
