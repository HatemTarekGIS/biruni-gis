"""Track statistics: distance (on the ellipsoid), durations, speeds and stops."""
from dataclasses import dataclass, field

from qgis.core import QgsCoordinateReferenceSystem, QgsDistanceArea, QgsPointXY, QgsProject

STOP_RADIUS_M = 25.0      # positions within this distance count as "not moving"
STOP_MIN_SECONDS = 60     # ... for at least this long = a stop
MOVING_KMH = 1.5          # below this speed a point does not count as moving


@dataclass
class Stop:
    start: object
    end: object
    lat: float
    lon: float

    @property
    def minutes(self):
        return round((self.end - self.start).total_seconds() / 60, 1)


@dataclass
class TrackStats:
    points: int = 0
    distance_m: float = 0.0
    duration_s: float = 0.0
    moving_s: float = 0.0
    max_kmh: float = 0.0
    start: object = None
    end: object = None
    stops: list = field(default_factory=list)
    speeds: list = field(default_factory=list)   # km/h per point (computed when missing)

    @property
    def avg_kmh(self):
        return round(self.distance_m / self.duration_s * 3.6, 2) if self.duration_s else 0.0

    @property
    def moving_avg_kmh(self):
        return round(self.distance_m / self.moving_s * 3.6, 2) if self.moving_s else 0.0


def distance_area():
    da = QgsDistanceArea()
    da.setSourceCrs(QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance().transformContext())
    da.setEllipsoid("EPSG:7030")  # WGS 84
    return da


def analyse(points, da=None):
    """points: list of (time: datetime, lat, lon, speed_kmh or None), time-ordered."""
    da = da or distance_area()
    st = TrackStats(points=len(points))
    if not points:
        return st
    st.start, st.end = points[0][0], points[-1][0]
    st.duration_s = max((st.end - st.start).total_seconds(), 0.0)
    st.speeds = [points[0][3] or 0.0]
    for (t0, la0, lo0, _), (t1, la1, lo1, v1) in zip(points, points[1:]):
        d = da.measureLine(QgsPointXY(lo0, la0), QgsPointXY(lo1, la1))
        dt_s = (t1 - t0).total_seconds()
        st.distance_m += d
        v = v1 if v1 is not None else (d / dt_s * 3.6 if dt_s > 0 else 0.0)
        st.speeds.append(round(v, 2))
        st.max_kmh = max(st.max_kmh, v)
        if v >= MOVING_KMH and dt_s > 0:
            st.moving_s += dt_s

    anchor = 0
    for i in range(1, len(points) + 1):
        far = i == len(points) or da.measureLine(
            QgsPointXY(points[anchor][2], points[anchor][1]), QgsPointXY(points[i][2], points[i][1])) > STOP_RADIUS_M
        if far:
            # the stop itself = the slow part of the cluster (not the approach / departure)
            slow = [j for j in range(anchor, i) if st.speeds[j] < MOVING_KMH]
            if slow:
                j0, j1 = max(slow[0] - 1, anchor), slow[-1]
                t_start, t_end = points[j0][0], points[j1][0]
                if (t_end - t_start).total_seconds() >= STOP_MIN_SECONDS:
                    group = points[j0:j1 + 1]
                    st.stops.append(Stop(t_start, t_end, sum(p[1] for p in group) / len(group),
                                         sum(p[2] for p in group) / len(group)))
            anchor = i
    st.max_kmh = round(st.max_kmh, 2)
    return st
