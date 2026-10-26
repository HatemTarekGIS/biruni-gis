"""Writes tracks into the project: GPS points, the track line with its
statistics, and the stops. Layers live in the project database (project
folder) or in temporary layers that are kept when the map is saved."""
import datetime as dt
import os
import uuid

from qgis.core import (QgsCoordinateReferenceSystem, QgsFeature, QgsField, QgsFields, QgsGeometry, QgsLineSymbol,
                       QgsMarkerSymbol, QgsPointXY, QgsProject, QgsSingleSymbolRenderer, QgsVectorFileWriter,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import QMetaType

from ..i18n import tr
from .analysis import analyse, distance_area

ROLE_KEY = "biruni/gps-role"
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")

LAYERS = {  # role: (table, geometry, [(field, type)])
    "points": ("gps_points", "Point", [
        ("track_id", "s"), ("time", "s"), ("lat", "d"), ("lon", "d"), ("alt", "d"), ("speed_kmh", "d"),
        ("course", "d"), ("satellites", "i"), ("hdop", "d"), ("accuracy_m", "d")]),
    "tracks": ("gps_tracks", "LineString", [
        ("track_id", "s"), ("name", "s"), ("start", "s"), ("end", "s"), ("duration_min", "d"),
        ("moving_min", "d"), ("distance_km", "d"), ("avg_kmh", "d"), ("moving_avg_kmh", "d"),
        ("max_kmh", "d"), ("points", "i"), ("stops", "i"), ("source", "s")]),
    "stops": ("gps_stops", "Point", [
        ("track_id", "s"), ("start", "s"), ("end", "s"), ("minutes", "d"), ("lat", "d"), ("lon", "d")]),
}
TITLES = {"points": "GPS points", "tracks": "GPS tracks", "stops": "GPS stops"}
_TYPES = {"s": QMetaType.Type.QString, "d": QMetaType.Type.Double, "i": QMetaType.Type.Int}


def _fields(spec):
    f = QgsFields()
    for name, t in spec:
        f.append(QgsField(name, _TYPES[t]))
    return f


def _iso(t):
    return t.astimezone().replace(microsecond=0).isoformat(sep=" ") if t else ""


def _feature(layer, geometry, values):
    """Feature with attributes set by field name (GeoPackage layers start with an "fid" field)."""
    f = QgsFeature(layer.fields())
    f.setGeometry(geometry)
    for name, value in values.items():
        f.setAttribute(name, value)
    return f


def _named(layer, values):
    fields = layer.fields()
    return {fields.indexOf(k): v for k, v in values.items() if fields.indexOf(k) >= 0}


def _style(role, layer, accent="#0E7C74"):
    if role == "points":
        sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": "#5B6670", "size": "1.2",
                                            "outline_style": "no"})
    elif role == "tracks":
        sym = QgsLineSymbol.createSimple({"color": accent, "width": "0.9", "capstyle": "round",
                                          "joinstyle": "round"})
    else:
        sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": "#F59E0B", "size": "4",
                                            "outline_color": "#ffffff", "outline_width": "0.5"})
    layer.setRenderer(QgsSingleSymbolRenderer(sym))


def ensure_layers():
    """The three GPS layers of the current project (created when missing)."""
    from ..project import project_gpkg

    prj = QgsProject.instance()
    found = {lyr.customProperty(ROLE_KEY): lyr for lyr in prj.mapLayers().values()
             if isinstance(lyr, QgsVectorLayer) and lyr.customProperty(ROLE_KEY)}
    gpkg = project_gpkg()
    group = None
    for role, (table, geom, spec) in LAYERS.items():
        if role in found and found[role].isValid():
            continue
        name = tr(TITLES[role])
        layer = None
        if gpkg:
            candidate = QgsVectorLayer(f"{gpkg}|layername={table}", name, "ogr")
            if not candidate.isValid():
                mem = QgsVectorLayer(f"{geom}?crs=EPSG:4326", table, "memory")
                mem.dataProvider().addAttributes(_fields(spec).toList())
                mem.updateFields()
                opts = QgsVectorFileWriter.SaveVectorOptions()
                opts.driverName, opts.layerName = "GPKG", table
                opts.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer
                QgsVectorFileWriter.writeAsVectorFormatV3(mem, gpkg, prj.transformContext(), opts)
                candidate = QgsVectorLayer(f"{gpkg}|layername={table}", name, "ogr")
            if candidate.isValid():
                layer = candidate
        if layer is None:
            layer = QgsVectorLayer(f"{geom}?crs=EPSG:4326", name, "memory")
            layer.dataProvider().addAttributes(_fields(spec).toList())
            layer.updateFields()
        layer.setCustomProperty(ROLE_KEY, role)
        _style(role, layer)
        if group is None:
            root = prj.layerTreeRoot()
            group = root.findGroup(tr("GPS")) or root.insertGroup(0, tr("GPS"))
        prj.addMapLayer(layer, False)
        group.insertLayer(0 if role != "points" else len(group.children()), layer)
        found[role] = layer
    return found


class TrackRecorder:
    """Records one track at a time from incoming fixes."""

    MAX_ACCURACY_M = 30.0   # ignore fixes worse than this
    MIN_STEP_M = 1.0        # ignore jitter smaller than this ...
    MAX_GAP_S = 10          # ... unless this long has passed (keeps stops measurable)

    def __init__(self):
        self.layers = None
        self.track_id = None
        self.name = ""
        self.points = []        # (time, lat, lon, speed)
        self.line_fid = None
        self.source = ""

    @property
    def recording(self):
        return self.track_id is not None

    def start(self, name, source=""):
        self.layers = ensure_layers()
        self.track_id = uuid.uuid4().hex[:8]
        self.name = name
        self.source = source
        self.points = []
        self.line_fid = None
        self._da = distance_area()
        self.distance_m = 0.0

    def add(self, fix):
        if not self.recording or not fix.valid:
            return False
        if fix.accuracy_m is not None and fix.accuracy_m > self.MAX_ACCURACY_M:
            return False
        if self.points:
            t0, la0, lo0, _ = self.points[-1]
            step = self._da.measureLine(QgsPointXY(lo0, la0), QgsPointXY(fix.lon, fix.lat))
            if step < self.MIN_STEP_M and (fix.time - t0).total_seconds() < self.MAX_GAP_S:
                return False
            self.distance_m += step
        self.points.append((fix.time, fix.lat, fix.lon, fix.speed_kmh))
        lyr = self.layers["points"]
        f = _feature(lyr, QgsGeometry.fromPointXY(QgsPointXY(fix.lon, fix.lat)), {
            "track_id": self.track_id, "time": _iso(fix.time), "lat": fix.lat, "lon": fix.lon, "alt": fix.alt,
            "speed_kmh": fix.speed_kmh, "course": fix.course, "satellites": fix.satellites, "hdop": fix.hdop,
            "accuracy_m": fix.accuracy_m})
        lyr.dataProvider().addFeatures([f])
        if len(self.points) < 50 or len(self.points) % 5 == 0:  # statistics are O(n): refresh less often
            self._update_line()
        lyr.triggerRepaint()
        return True

    def _line_geometry(self):
        return QgsGeometry.fromPolylineXY([QgsPointXY(p[2], p[1]) for p in self.points])

    def _update_line(self, stats=None):
        if len(self.points) < 2:
            return
        lyr = self.layers["tracks"]
        st = stats or analyse(self.points)
        attrs = {"track_id": self.track_id, "name": self.name, "start": _iso(st.start), "end": _iso(st.end),
                 "duration_min": round(st.duration_s / 60, 1), "moving_min": round(st.moving_s / 60, 1),
                 "distance_km": round(st.distance_m / 1000, 3), "avg_kmh": st.avg_kmh,
                 "moving_avg_kmh": st.moving_avg_kmh, "max_kmh": st.max_kmh, "points": st.points,
                 "stops": len(st.stops), "source": self.source}
        if self.line_fid is None:
            f = _feature(lyr, self._line_geometry(), attrs)
            ok, out = lyr.dataProvider().addFeatures([f])
            self.line_fid = out[0].id() if ok and out else None
        else:
            pr = lyr.dataProvider()
            pr.changeGeometryValues({self.line_fid: self._line_geometry()})
            pr.changeAttributeValues({self.line_fid: _named(lyr, attrs)})
        lyr.triggerRepaint()

    def stop(self):
        """Finish the track; returns its statistics (None if nothing was recorded)."""
        if not self.recording:
            return None
        st = analyse(self.points) if self.points else None
        if st and len(self.points) >= 2:
            self._update_line(st)
            lyr = self.layers["stops"]
            feats = []
            for s in st.stops:
                feats.append(_feature(lyr, QgsGeometry.fromPointXY(QgsPointXY(s.lon, s.lat)), {
                    "track_id": self.track_id, "start": _iso(s.start), "end": _iso(s.end), "minutes": s.minutes,
                    "lat": s.lat, "lon": s.lon}))
            if feats:
                lyr.dataProvider().addFeatures(feats)
                lyr.triggerRepaint()
        self.track_id = None
        return st


def default_track_name():
    return tr("Track {d}").format(d=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))


def write_gpx_points(points, path):
    """Not used by the UI; handy for tests: write (time, lat, lon) as GPX."""
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0"?><gpx version="1.1" creator="Biruni GIS"><trk><trkseg>')
        for t, la, lo, *_ in points:
            f.write(f'<trkpt lat="{la}" lon="{lo}"><time>{t.strftime("%Y-%m-%dT%H:%M:%SZ")}</time></trkpt>')
        f.write("</trkseg></trk></gpx>")
    return os.path.exists(path)
