"""Pure-Python dead-reckoning constants and flat-earth extrapolation matching DR_JS."""
import math

DRC = {
    "MIN_M": 5,
    "ACC_K": 2,
    "ACC_CAP_M": 50,
    "TURN_DEG": 25,
    "MOVE_MS": 0.8,
    "STOP_MS": 0.4,
    "HEARTBEAT_MS": 30000,
    "EXTRAP_MS": 10000,
    "VEL_GAP_MS": 30000,
}

METERS_PER_DEG = 111320.0


def predict(lat, lon, speed_ms, cog, dt_s):
    """Extrapolate (lat, lon) along cog (degrees clockwise from north) at speed_ms for dt_s."""
    if speed_ms is None or cog is None or dt_s is None:
        return lat, lon
    try:
        v = float(speed_ms)
        c = float(cog)
        dt = float(dt_s)
    except (TypeError, ValueError):
        return lat, lon
    if not (math.isfinite(v) and math.isfinite(c) and math.isfinite(dt)) or v <= 0.0 or dt <= 0.0:
        return lat, lon
    dt = min(dt, DRC["EXTRAP_MS"] / 1000.0)
    dist = v * dt
    rad = math.radians(c)
    dlat = (dist * math.cos(rad)) / METERS_PER_DEG
    cos_lat = max(1e-6, abs(math.cos(math.radians(lat))))
    dlon = (dist * math.sin(rad)) / (METERS_PER_DEG * cos_lat)
    return lat + dlat, lon + dlon
