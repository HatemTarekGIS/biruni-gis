"""«موعد الانتهاء»: when each zone (and the whole job) is expected to finish, with an 80 % interval.

The input is a series of (day timestamp, done) per zone - from the progress widget's daily history (showkit) - and
the zone's total. The speed is the Theil–Sen slope of done over the last WINDOW days (robust to one big day and to
days nobody worked); its uncertainty comes from treating the daily increments as a Poisson count: the rate's
posterior is Gamma(sum + ½, days), and the time to finish the remaining R items adds the process noise of a Poisson
process (Var T = R / λ²). The interval is the normal approximation of T: mean R/λ, variance R/λ² + R²·Var(λ)/λ⁴.

finish(series, total, now) -> {"rate", "remaining", "eta", "lo", "hi", "done"} (timestamps, seconds); None when
there is too little history (fewer than 3 days) or no progress.
"""
import math

WINDOW = 21           # days of history the speed is measured on
Z80 = 1.2815515655446004
DAY = 86400.0


def theil_sen(points):
    """The median of pairwise slopes of [(x, y)]."""
    slopes = []
    n = len(points)
    for i in range(n):
        for j in range(i + 1, n):
            dx = points[j][0] - points[i][0]
            if dx:
                slopes.append((points[j][1] - points[i][1]) / dx)
    if not slopes:
        return None
    slopes.sort()
    m = len(slopes)
    return slopes[m // 2] if m % 2 else 0.5 * (slopes[m // 2 - 1] + slopes[m // 2])


def finish(series, total, now=None):
    pts = sorted((float(t), float(v)) for t, v in series if t is not None and v is not None)
    if not pts or total is None:
        return None
    done = pts[-1][1]
    remaining = max(0.0, float(total) - done)
    now = now or pts[-1][0]
    if remaining <= 0:
        return {"rate": 0.0, "remaining": 0, "done": True, "eta": pts[-1][0], "lo": pts[-1][0], "hi": pts[-1][0]}
    pts = [p for p in pts if p[0] >= pts[-1][0] - WINDOW * DAY]
    if len(pts) < 3:
        return None
    days = [(t / DAY, v) for t, v in pts]
    slope = theil_sen(days)
    span_days = days[-1][0] - days[0][0]
    gained = max(0.0, days[-1][1] - days[0][1])
    if span_days <= 0:
        return None
    # the Poisson rate's posterior (per day) and its spread
    shape, rate_par = gained + 0.5, span_days
    lam_mean = shape / rate_par
    lam = slope if slope and slope > 0 else lam_mean
    if lam <= 0 or gained <= 0:
        return {"rate": 0.0, "remaining": remaining, "done": False, "eta": None, "lo": None, "hi": None}
    var_lam = shape / rate_par ** 2
    t_mean = remaining / lam
    var_t = remaining / lam ** 2 + remaining ** 2 * var_lam / lam ** 4
    sd = math.sqrt(var_t)
    lo = max(0.0, t_mean - Z80 * sd)
    hi = t_mean + Z80 * sd
    base = max(now, pts[-1][0])
    return {"rate": lam, "remaining": remaining, "done": False, "eta": base + t_mean * DAY,
            "lo": base + lo * DAY, "hi": base + hi * DAY}


def cone(series, total, f, steps=12):
    """The burn-up chart's points: history [(t, done)] and the cone [(t, low, mid, high)] from now to the late end."""
    pts = sorted((float(t), float(v)) for t, v in series)
    if not pts or not f or not f.get("eta"):
        return pts, []
    t0, d0 = pts[-1]
    end = f["hi"]
    out = []
    for i in range(steps + 1):
        t = t0 + (end - t0) * i / steps
        dt = (t - t0) / DAY
        mid = min(total, d0 + f["rate"] * dt)
        # the cone: the fast and slow speeds that give the interval's ends
        fast = f["remaining"] / max(1e-9, (f["lo"] - t0) / DAY) if f["lo"] > t0 else f["rate"] * 2
        slow = f["remaining"] / max(1e-9, (f["hi"] - t0) / DAY)
        out.append([t, min(total, d0 + slow * dt), mid, min(total, d0 + fast * dt)])
    return pts, out


def zones(hist, wid):
    """From showkit history: {zone key: [(ts, done)]}, {zone key: total}, {zone key: label}, xf field."""
    series, totals, labels, xf = {}, {}, {}, None
    for _day, ts, summ in hist:
        s = summ.get(wid) or {}
        if s.get("type") != "progress":
            continue
        xf = s.get("xf") or xf
        for k, (done, total) in (s.get("prog") or {}).items():
            series.setdefault(k, []).append((ts, done))
            totals[k] = total
            labels[k] = (s.get("labels") or {}).get(k, k)
    return series, totals, labels, xf


def forecast_of(hist, wid, current=None, now=None):
    """The forecast widget's data: {"zones": [...], "overall": {...}, "xf"}."""
    series, totals, labels, xf = zones(hist, wid)
    if current:   # today's numbers (may be newer than the last history entry)
        for k, (done, total) in (current.get("prog") or {}).items():
            s = series.setdefault(k, [])
            t = now or (s[-1][0] + 1 if s else 0)
            if s and abs(s[-1][0] - t) < 3600:
                s[-1] = (t, done)
            else:
                s.append((t, done))
            totals[k] = total
            labels[k] = (current.get("labels") or {}).get(k, k)
        xf = current.get("xf") or xf
    out = []
    for k in series:
        f = finish(series[k], totals.get(k), now)
        out.append({"key": k, "label": labels.get(k, k), "done": series[k][-1][1] if series[k] else 0,
                    "total": totals.get(k), "rate": f and round(f["rate"], 3), "eta": f and f["eta"],
                    "lo": f and f["lo"], "hi": f and f["hi"], "finished": bool(f and f.get("done"))})
    # the whole job: the sum of the zones day by day
    by_t = {}
    for k, s in series.items():
        for t, v in s:
            day = int(t // DAY)
            by_t.setdefault(day, {})[k] = (t, v)
    tot = sum(v for v in totals.values() if v)
    agg, last = [], {}
    for day in sorted(by_t):
        last.update({k: v for k, (_t, v) in by_t[day].items()})
        agg.append((max(t for t, _v in by_t[day].values()), sum(last.values())))
    f = finish(agg, tot, now)
    hist_pts, cone_pts = cone(agg, tot, f)
    overall = {"done": agg[-1][1] if agg else 0, "total": tot, "rate": f and round(f["rate"], 3),
               "eta": f and f["eta"], "lo": f and f["lo"], "hi": f and f["hi"], "finished": bool(f and f.get("done")),
               "series": [[round(t), v] for t, v in hist_pts], "cone": [[round(c[0])] + c[1:] for c in cone_pts]}
    return {"zones": out, "overall": overall, "xf": xf}
