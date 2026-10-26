import datetime as dt
import os

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink,
                       QgsField, QgsFields, QgsGeometry, QgsPointXY, QgsProcessingException,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFile, QgsVectorLayer)
from qgis.PyQt.QtCore import QDateTime, QMetaType

from ..gps.analysis import analyse
from ..i18n import tr
from .base import BiruniAlgorithm

TIME_FIELDS = ("time", "timestamp", "datetime", "date_time", "begin", "when")


def _to_datetime(v):
    if isinstance(v, QDateTime):
        return v.toPyDateTime() if v.isValid() else None
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, str) and v:
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


class AnalyseTrack(BiruniAlgorithm):
    ID = "analysetrack"
    NAME = "Import and analyse a GPS track"
    GROUP = "GPS & field"
    GROUP_ID = "gps"
    HELP = ("Reads a track recorded with a phone or GPS receiver (GPX, or KML/GeoJSON points with a time) and "
            "produces the track line with its statistics - distance, duration, moving time, average and "
            "maximum speed - the points with their speed, and the places where the device stopped for a "
            "minute or more.")

    INPUT, POINTS, TRACK, STOPS = "INPUT", "POINTS", "TRACK", "STOPS"

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile(
            self.INPUT, tr("Track file"), fileFilter="GPX (*.gpx);;KML (*.kml *.kmz);;GeoJSON (*.geojson *.json);;"
                                                     "All files (*.*)"))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.TRACK, tr("Track"), Qgis.ProcessingSourceType.VectorLine))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.POINTS, tr("Track points"), Qgis.ProcessingSourceType.VectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.STOPS, tr("Stops"), Qgis.ProcessingSourceType.VectorPoint))

    def _open(self, path):
        if path.lower().endswith(".gpx"):
            lyr = QgsVectorLayer(f"{path}|layername=track_points", "pts", "ogr")
            if lyr.isValid() and lyr.featureCount() > 0:
                return lyr
            lyr = QgsVectorLayer(f"{path}|layername=route_points", "pts", "ogr")
            if lyr.isValid() and lyr.featureCount() > 0:
                return lyr
        lyr = QgsVectorLayer(path, "pts", "ogr")
        return lyr if lyr.isValid() else None

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        path = self.parameterAsFile(parameters, self.INPUT, context)
        src = self._open(path)
        if src is None:
            raise QgsProcessingException(tr("Could not read {p}").format(p=path))
        to_wgs = QgsCoordinateTransform(src.crs(), QgsCoordinateReferenceSystem("EPSG:4326"),
                                        context.transformContext())
        names = [f.name() for f in src.fields()]
        tfield = next((n for n in names if n.lower() in TIME_FIELDS), None)
        pts = []
        for f in src.getFeatures():
            g = f.geometry()
            if g.isNull() or g.isEmpty():
                continue
            for v in g.vertices():  # points, or the vertices of a line without times
                p = to_wgs.transform(QgsPointXY(v.x(), v.y()))
                t = _to_datetime(f[tfield]) if tfield else None
                pts.append((t, p.y(), p.x(), None))
        if not pts:
            raise QgsProcessingException(tr("The file contains no track points."))
        timed = all(p[0] is not None for p in pts)
        if timed:
            pts.sort(key=lambda p: p[0])
        else:
            feedback.pushWarning(tr("The points have no time: only the distance can be measured."))
            base = dt.datetime(2000, 1, 1)
            pts = [(base, la, lo, v) for _, la, lo, v in pts]
        st = analyse(pts)

        crs = QgsCoordinateReferenceSystem("EPSG:4326")
        pf = QgsFields()
        for n, t in (("time", QMetaType.Type.QString), ("lat", QMetaType.Type.Double),
                     ("lon", QMetaType.Type.Double), ("speed_kmh", QMetaType.Type.Double)):
            pf.append(QgsField(n, t))
        psink, pid = self.parameterAsSink(parameters, self.POINTS, context, pf, Qgis.WkbType.Point, crs)
        for (t, la, lo, _), v in zip(pts, st.speeds):
            o = QgsFeature(pf)
            o.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lo, la)))
            o.setAttributes([t.isoformat(sep=" ") if timed else None, la, lo, v if timed else None])
            psink.addFeature(o, QgsFeatureSink.Flag.FastInsert)

        tf = QgsFields()
        for n, t in (("name", QMetaType.Type.QString), ("start", QMetaType.Type.QString),
                     ("end", QMetaType.Type.QString), ("duration_min", QMetaType.Type.Double),
                     ("moving_min", QMetaType.Type.Double), ("distance_km", QMetaType.Type.Double),
                     ("avg_kmh", QMetaType.Type.Double), ("moving_avg_kmh", QMetaType.Type.Double),
                     ("max_kmh", QMetaType.Type.Double), ("points", QMetaType.Type.Int),
                     ("stops", QMetaType.Type.Int)):
            tf.append(QgsField(n, t))
        tsink, tid = self.parameterAsSink(parameters, self.TRACK, context, tf, Qgis.WkbType.LineString, crs)
        o = QgsFeature(tf)
        o.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(p[2], p[1]) for p in pts]))
        o.setAttributes([os.path.splitext(os.path.basename(path))[0],
                         st.start.isoformat(sep=" ") if timed else None, st.end.isoformat(sep=" ") if timed else None,
                         round(st.duration_s / 60, 1) if timed else None, round(st.moving_s / 60, 1) if timed else None,
                         round(st.distance_m / 1000, 3), st.avg_kmh if timed else None,
                         st.moving_avg_kmh if timed else None, st.max_kmh if timed else None, st.points,
                         len(st.stops) if timed else None])
        tsink.addFeature(o, QgsFeatureSink.Flag.FastInsert)

        sf = QgsFields()
        for n, t in (("start", QMetaType.Type.QString), ("end", QMetaType.Type.QString),
                     ("minutes", QMetaType.Type.Double)):
            sf.append(QgsField(n, t))
        ssink, sid = self.parameterAsSink(parameters, self.STOPS, context, sf, Qgis.WkbType.Point, crs)
        for s in (st.stops if timed else []):
            o = QgsFeature(sf)
            o.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(s.lon, s.lat)))
            o.setAttributes([s.start.isoformat(sep=" "), s.end.isoformat(sep=" "), s.minutes])
            ssink.addFeature(o, QgsFeatureSink.Flag.FastInsert)

        feedback.pushInfo(tr("{n} points, {km} km, {stops} stops.").format(
            n=st.points, km=round(st.distance_m / 1000, 3), stops=len(st.stops) if timed else 0))
        return {self.TRACK: tid, self.POINTS: pid, self.STOPS: sid, "DISTANCE_KM": round(st.distance_m / 1000, 3),
                "STOP_COUNT": len(st.stops) if timed else 0}
