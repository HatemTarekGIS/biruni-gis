"""Emerging hot spots: events counted in hexagons x time steps (a space-time cube), Getis-Ord Gi* on
the cube (neighbours in space within a distance, in the same and the previous k steps), the Mann-Kendall
trend of each place's z-scores, and the classic categories (new, consecutive, intensifying, persistent,
diminishing, sporadic, oscillating, historical hot or cold spot, no pattern) as defined for ArcGIS Pro's
Emerging Hot Spot Analysis."""
import numpy as np

from . import weights as wts
from .autocorr import mann_kendall


# scipy is imported on first use: Biruni imports every tool module at start-up
def _sparse():
    from scipy import sparse

    return sparse


def _stats():
    from scipy import stats

    return stats

# category codes (stored in the output layer); hot ones positive, cold ones negative
PATTERN_CODES = ["no_pattern", "new", "consecutive", "intensifying", "persistent", "diminishing", "sporadic",
                 "oscillating", "historical"]
HOT_COLOURS = {"new": "#F4A6A6", "consecutive": "#E97B7B", "intensifying": "#C0392B", "persistent": "#D35454",
               "diminishing": "#E8B4A0", "sporadic": "#F2C3A0", "oscillating": "#D98C6C",
               "historical": "#B98282"}
COLD_COLOURS = {"new": "#A9CBEA", "consecutive": "#7EAEDB", "intensifying": "#1F5FA8", "persistent": "#3E7CC0",
                "diminishing": "#A8BFD6", "sporadic": "#A6D3D6", "oscillating": "#6C9FBF",
                "historical": "#8296B5"}
NO_PATTERN_COLOUR = "#E6E6E6"
STEP_UNITS = ["auto", "h", "D", "W", "M", "Q", "Y"]


def to_steps(times, unit):
    """times: numpy datetime64[s] -> (step index from 0, step start labels). Calendar months/years."""
    t = np.asarray(times, dtype="datetime64[s]")
    if unit in ("M", "Q", "Y"):
        months = t.astype("datetime64[M]").astype(np.int64)
        span = {"M": 1, "Q": 3, "Y": 12}[unit]
        base = months.min() - (months.min() % span if unit != "M" else 0)
        idx = (months - base) // span
        starts = (base + np.arange(idx.max() + 1) * span).astype("datetime64[M]")
        return idx, starts.astype("datetime64[D]")
    sec = {"h": 3600, "D": 86400, "W": 7 * 86400}[unit]
    secs = t.astype(np.int64)
    base = secs.min() - secs.min() % (sec if unit != "W" else 86400)
    idx = (secs - base) // sec
    starts = (base + np.arange(idx.max() + 1) * sec).astype("datetime64[s]")
    return idx, starts


def auto_unit(times):
    """The step that gives 10-40 time steps where possible (the trend test needs at least 10)."""
    t = np.asarray(times, dtype="datetime64[s]")
    span = float((t.max() - t.min()).astype(np.int64))
    for unit, sec in (("h", 3600), ("D", 86400), ("W", 7 * 86400), ("M", 30.44 * 86400), ("Q", 91.3 * 86400),
                      ("Y", 365.25 * 86400)):
        if span / sec <= 40:
            return unit
    return "Y"


def cube(xy, steps, width):
    """Counts per (hexagon with at least one event, time step)."""
    grid = wts.hex_cells(xy, width, fill_hull=False)
    m = len(grid["centres"])
    t = int(steps.max()) + 1
    counts = np.zeros((m, t))
    np.add.at(counts, (grid["point_cell"], steps), 1.0)
    return grid, counts


def space_time_gi(counts, w_space, k_previous=1):
    """Gi* for every bin of the cube: binary neighbours = spatial neighbours (and itself) in this step and
    the previous k steps; mean and spread taken over the whole cube. -> z, p (cells x steps)."""
    x = np.asarray(counts, dtype=float)
    m, t = x.shape
    ws = wts.with_self(wts.binary(w_space)) if w_space.nnz else _sparse().identity(m, format="csr")
    lag = ws @ x  # spatial sum in each step
    c = np.cumsum(lag, axis=1)
    lo = np.arange(t) - k_previous - 1
    window = c - np.where(lo >= 0, c[:, np.clip(lo, 0, None)], 0.0)
    steps_in = np.minimum(np.arange(t) + 1, k_previous + 1)
    card = np.asarray(ws.sum(1)).ravel()
    wsum = card[:, None] * steps_in[None, :]
    n = x.size
    xbar = x.mean()
    s = np.sqrt((x * x).mean() - xbar * xbar)
    den = s * np.sqrt(np.maximum(n * wsum - wsum * wsum, 0) / (n - 1))
    z = np.divide(window - xbar * wsum, den, out=np.zeros_like(window), where=den > 0)
    return z, 2 * _stats().norm.sf(np.abs(z))


def significant(z, p, use_fdr=True, alpha=0.10):
    """+1 hot, -1 cold, 0 not significant (90% confidence; FDR within each time step)."""
    from .common import fdr

    out = np.zeros(z.shape, dtype=int)
    for k in range(z.shape[1]):
        sig = fdr(p[:, k], alpha) if use_fdr else p[:, k] <= alpha
        out[sig, k] = np.sign(z[sig, k]).astype(int)
    return out


def classify(sig, z, trend_alpha=0.05):
    """Category of every place from its series of hot (+1) / cold (-1) / nothing (0) and the Mann-Kendall
    trend of its z-scores. -> (code list, sign +1 hot / -1 cold / 0, trend z, trend p)."""
    m, t = sig.shape
    _, tz, tp = mann_kendall(z)
    codes, sides = [], []
    for r in range(m):
        s = sig[r]
        code, side = "no_pattern", 0
        for sd in (1, -1):
            this = s == sd
            other = s == -sd
            share = this.mean()
            final = bool(this[-1])
            trend_up = tp[r] < trend_alpha and (tz[r] * sd) > 0
            trend_down = tp[r] < trend_alpha and (tz[r] * sd) < 0
            if final:
                if share >= 0.9:
                    code = "intensifying" if trend_up else "diminishing" if trend_down else "persistent"
                elif this.sum() == 1:
                    code = "new"
                else:
                    run = 0
                    while run < t and this[t - 1 - run]:
                        run += 1
                    if run >= 2 and not this[:t - run].any():
                        code = "consecutive"
                    elif other[:-1].any():
                        code = "oscillating"
                    else:
                        code = "sporadic"
                side = sd
                break
            if share >= 0.9:
                code, side = "historical", sd
                break
        codes.append(code)
        sides.append(side)
    return codes, np.array(sides), tz, tp
