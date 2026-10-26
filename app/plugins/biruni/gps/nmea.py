"""Minimal, dependency-free NMEA 0183 reader (RMC, GGA, GSA, VTG).

Phones and GNSS receivers send one burst of sentences per fix ("epoch"); the
sentences of an epoch share the same UTC time. The parser merges them and
emits one Fix per epoch.
"""
import datetime as dt
from dataclasses import dataclass, field

KNOTS_TO_KMH = 1.852
UERE_M = 5.0  # typical user-equivalent range error: accuracy ~ HDOP x 5 m


@dataclass
class Fix:
    time: dt.datetime                 # UTC
    lat: float
    lon: float
    alt: float = None
    speed_kmh: float = None
    course: float = None
    satellites: int = None
    hdop: float = None
    quality: int = None               # GGA fix quality (0 = none, 1 = GPS, 2 = DGPS, 4/5 = RTK)
    valid: bool = True
    sentences: set = field(default_factory=set)

    @property
    def accuracy_m(self):
        return round(self.hdop * UERE_M, 1) if self.hdop else None


def checksum_ok(sentence):
    if "*" not in sentence:
        return True  # some apps omit it
    body, _, given = sentence[1:].partition("*")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(given[:2], 16)
    except ValueError:
        return False


def _coord(value, hemi):
    if not value:
        return None
    dot = value.find(".")
    deg_len = (dot if dot >= 0 else len(value)) - 2
    try:
        deg = float(value[:deg_len])
        minutes = float(value[deg_len:])
    except ValueError:
        return None
    v = deg + minutes / 60.0
    return -v if hemi in ("S", "W") else v


def _float(v):
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def _int(v):
    try:
        return int(v) if v not in ("", None) else None
    except ValueError:
        return None


def _time(hhmmss, date):
    if not hhmmss or len(hhmmss) < 6:
        return None
    try:
        h, m = int(hhmmss[0:2]), int(hhmmss[2:4])
        s = float(hhmmss[4:])
        base = dt.datetime.combine(date, dt.time(h, m, int(s)), tzinfo=dt.timezone.utc)
        return base + dt.timedelta(microseconds=round((s - int(s)) * 1e6))
    except ValueError:
        return None


class NmeaParser:
    def __init__(self):
        self._buffer = ""
        self._date = None          # last date seen in RMC
        self._epoch = None         # time string of the epoch being assembled
        self._data = {}
        self._emitted = False
        self.last_hdop = None
        self.last_sats = None

    def feed(self, text):
        """Feed raw text (any chunking); returns the list of completed fixes."""
        self._buffer += text
        out = []
        *lines, self._buffer = self._buffer.replace("\r", "\n").split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith(("$", "!")) and checksum_ok(line):
                fix = self._sentence(line.split("*")[0])
                if fix is not None:
                    out.append(fix)
        return out

    def flush(self):
        """Emit the epoch still being assembled (e.g. at end of a file)."""
        return [f for f in [self._emit()] if f is not None]

    # ---- internals -----------------------------------------------------
    def _sentence(self, line):
        parts = line.split(",")
        kind = parts[0][3:] if len(parts[0]) >= 6 else ""
        if kind == "GSA":
            if len(parts) > 16:
                self.last_hdop = _float(parts[16]) or self.last_hdop
            return None
        if kind == "VTG":
            if self._data and len(parts) > 7:
                self._data.setdefault("course", _float(parts[1]))
                self._data.setdefault("speed_kmh", _float(parts[7]))
            return None
        if kind not in ("RMC", "GGA"):
            return None
        epoch = parts[1] if len(parts) > 1 else ""
        out = None
        if self._epoch is not None and epoch != self._epoch:
            out = self._emit()
        if epoch != self._epoch:
            self._epoch, self._data, self._emitted = epoch, {}, False
        d = self._data
        if kind == "RMC" and len(parts) > 9:
            if parts[9] and len(parts[9]) == 6:
                try:
                    yy = int(parts[9][4:6])
                    self._date = dt.date((1900 if yy >= 80 else 2000) + yy, int(parts[9][2:4]),
                                         int(parts[9][0:2]))
                except ValueError:
                    pass
            d["rmc"] = True
            d["valid_rmc"] = parts[2] == "A"
            d["lat"] = _coord(parts[3], parts[4])
            d["lon"] = _coord(parts[5], parts[6])
            knots = _float(parts[7])
            d["speed_kmh"] = round(knots * KNOTS_TO_KMH, 2) if knots is not None else d.get("speed_kmh")
            d["course"] = _float(parts[8]) if _float(parts[8]) is not None else d.get("course")
        elif kind == "GGA" and len(parts) > 9:
            d["gga"] = True
            d["quality"] = _int(parts[6])
            d.setdefault("lat", _coord(parts[2], parts[3]))
            d.setdefault("lon", _coord(parts[4], parts[5]))
            d["satellites"] = _int(parts[7])
            d["hdop"] = _float(parts[8])
            d["alt"] = _float(parts[9])
            if d["satellites"] is not None:
                self.last_sats = d["satellites"]
        if d.get("rmc") and d.get("gga") and not self._emitted:
            fix = self._emit()
            return fix or out
        return out

    def _emit(self):
        d = self._data
        if self._emitted or not d or d.get("lat") is None or d.get("lon") is None:
            return None
        self._emitted = True
        valid = d.get("valid_rmc", True) and (d.get("quality") is None or d["quality"] > 0)
        date = self._date or dt.datetime.now(dt.timezone.utc).date()
        t = _time(self._epoch, date) or dt.datetime.now(dt.timezone.utc)
        return Fix(time=t, lat=d["lat"], lon=d["lon"], alt=d.get("alt"), speed_kmh=d.get("speed_kmh"),
                   course=d.get("course"), satellites=d.get("satellites", self.last_sats),
                   hdop=d.get("hdop", self.last_hdop), quality=d.get("quality"), valid=valid,
                   sentences={k for k in ("rmc", "gga") if d.get(k)})


def make_sentences(time_utc, lat, lon, speed_kmh=0.0, course=0.0, alt=0.0, sats=10, hdop=0.9):
    """RMC + GGA for one fix - used by the simulator and the tests."""
    def fmt(v, is_lat):
        hemi = ("N" if v >= 0 else "S") if is_lat else ("E" if v >= 0 else "W")
        v = abs(v)
        deg = int(v)
        minutes = (v - deg) * 60
        return (f"{deg:02d}{minutes:07.4f}" if is_lat else f"{deg:03d}{minutes:07.4f}"), hemi

    def wrap(body):
        cs = 0
        for ch in body:
            cs ^= ord(ch)
        return f"${body}*{cs:02X}\r\n"

    t = time_utc.strftime("%H%M%S.") + f"{time_utc.microsecond // 10000:02d}"
    d = time_utc.strftime("%d%m%y")
    la, lah = fmt(lat, True)
    lo, loh = fmt(lon, False)
    knots = speed_kmh / KNOTS_TO_KMH
    rmc = wrap(f"GPRMC,{t},A,{la},{lah},{lo},{loh},{knots:.2f},{course:.1f},{d},,,A")
    gga = wrap(f"GPGGA,{t},{la},{lah},{lo},{loh},1,{sats:02d},{hdop:.1f},{alt:.1f},M,0.0,M,,")
    return rmc + gga
