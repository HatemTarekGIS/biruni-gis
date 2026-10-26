"""NMEA parser and track analysis tests.

Run:  engine\\bin\\python-qgis.bat tests\\test_gps.py
"""
import datetime as dt
import os
import sys

from qgis.core import QgsApplication

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "plugins"))
app = QgsApplication([], False)
app.initQgis()
from biruni.gps.analysis import analyse  # noqa: E402
from biruni.gps.nmea import NmeaParser, checksum_ok, make_sentences  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


# real-world style sentences (u-blox / Android)
real = ("$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A\r\n"
        "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n")
check(checksum_ok(real.split("\r\n")[0]), "checksum of a known RMC")
p = NmeaParser()
fixes = p.feed(real[:37]) + p.feed(real[37:])  # arbitrary chunking
check(len(fixes) == 1, f"one fix per epoch, got {len(fixes)}")
f = fixes[0]
check(abs(f.lat - 48.1173) < 1e-4 and abs(f.lon - 11.516667) < 1e-5, f"lat/lon {f.lat}, {f.lon}")
check(abs(f.speed_kmh - 41.48) < 0.01, f"speed 22.4 kn = 41.48 km/h, got {f.speed_kmh}")
check(f.satellites == 8 and f.hdop == 0.9 and f.alt == 545.4 and f.accuracy_m == 4.5, "GGA fields merged")
check(f.time == dt.datetime(1994, 3, 23, 12, 35, 19, tzinfo=dt.timezone.utc), f"time {f.time}")
check(p.feed("$GPRMC,123520,A,4807.038,N,01131.000,E,0,0,230394,,*00\r\n") == [], "bad checksum ignored")

# southern / western hemispheres and an invalid fix
s = make_sentences(dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc), -33.8688, -151.2093)
f = NmeaParser().feed(s)[0]
check(abs(f.lat + 33.8688) < 1e-6 and abs(f.lon + 151.2093) < 1e-6, "S/W coordinates negative")
inv = "$GPRMC,010101,V,,,,,,,020126,,,N*"
cs = 0
for ch in inv[1:-1]:
    cs ^= ord(ch)
check(NmeaParser().feed(inv + f"{cs:02X}\r\n") == [], "no fix without coordinates")

# simulated walk: 10 min north at ~5 km/h, 3 min standing still, then 5 min east
t = dt.datetime(2026, 10, 24, 8, 0, tzinfo=dt.timezone.utc)
lat, lon = 30.0, 31.0
pts = []
for _ in range(600):  # 1 s steps, 1.39 m each
    pts.append((t, lat, lon, None))
    t += dt.timedelta(seconds=1)
    lat += 1.39 / 110850
for _ in range(180):
    pts.append((t, lat, lon, None))
    t += dt.timedelta(seconds=1)
for _ in range(300):
    pts.append((t, lat, lon, None))
    t += dt.timedelta(seconds=1)
    lon += 1.39 / 96490
st = analyse(pts)
check(1220 < st.distance_m < 1280, f"distance ~1250 m, got {st.distance_m:.1f}")
check(len(st.stops) == 1 and 2.9 <= st.stops[0].minutes <= 3.1, f"one 3-minute stop, got {[s.minutes for s in st.stops]}")
check(4.7 < st.moving_avg_kmh < 5.3, f"moving average ~5 km/h, got {st.moving_avg_kmh}")
check(abs(st.duration_s - 1079) < 1, f"duration 1079 s, got {st.duration_s}")

# ---- GPX import algorithm
import tempfile  # noqa: E402

sys.path.append(QgsApplication.prefixPath() + "/python/plugins")
import processing  # noqa: E402
from processing.core.Processing import Processing  # noqa: E402

from biruni.algorithms import BiruniProvider  # noqa: E402
from biruni.gps.recorder import write_gpx_points  # noqa: E402

Processing.initialize()
provider = BiruniProvider()
QgsApplication.processingRegistry().addProvider(provider)
gpx = os.path.join(tempfile.mkdtemp(), "walk.gpx")
write_gpx_points(pts[::5], gpx)  # every 5th second, like a phone app
r = processing.run("biruni:analysetrack", {"INPUT": gpx, "TRACK": "memory:", "POINTS": "memory:",
                                           "STOPS": "memory:"})
check(1.2 < r["DISTANCE_KM"] < 1.28, f"GPX distance {r['DISTANCE_KM']} km")
check(r["STOP_COUNT"] == 1, f"GPX stops {r['STOP_COUNT']}")
trk = next(r["TRACK"].getFeatures())
check(trk["points"] == len(pts[::5]) and trk["max_kmh"] > 4, f"track attributes {trk.attributes()}")

# ---- live UDP reception (what a phone app does)
from qgis.PyQt.QtCore import QEventLoop, QTimer  # noqa: E402
from qgis.PyQt.QtNetwork import QHostAddress, QUdpSocket  # noqa: E402

from biruni.gps.source import NmeaSource  # noqa: E402

src = NmeaSource()
got = []
src.fixReceived.connect(got.append)
check(src.listen(10111), "listening on UDP/TCP 10111")
sender = QUdpSocket()
t0 = dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.timezone.utc)
for k in range(5):
    data = make_sentences(t0 + dt.timedelta(seconds=k), 30.0 + k * 1e-5, 31.0).encode()
    sender.writeDatagram(data, QHostAddress(QHostAddress.SpecialAddress.LocalHost), 10111)
loop = QEventLoop()
QTimer.singleShot(1500, loop.quit)
loop.exec()
check(len(got) == 5, f"5 fixes received over UDP, got {len(got)}")
src.stop()

app.exitQgis()
print("\n" + ("ALL PASSED" if not failures else f"{len(failures)} FAILED"))
sys.exit(1 if failures else 0)
