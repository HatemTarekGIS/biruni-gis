"""Where points concentrate and how they spread: kernel density with an explained bandwidth, and the
central tendency and spread (mean and median centre, standard distance, standard deviational ellipse).

Pure numpy/scipy; coordinates in metres."""
import math

import numpy as np


# ---- kernel density ------------------------------------------------------------------------------------
def _wstd(v, w):
    m = np.average(v, weights=w)
    return math.sqrt(max(np.average((v - m) ** 2, weights=w), 0.0))


def effective_n(w):
    w = np.asarray(w, dtype=float)
    return float(w.sum() ** 2 / max((w * w).sum(), 1e-300))


def silverman_bandwidth(xy, w=None):
    """Silverman's / Scott's rule for a 2-D Gaussian kernel: h = sigma * n^(-1/6), where sigma is the
    average of the two axes' spreads, each the smaller of the standard deviation and IQR/1.349 (robust to
    outliers), and n the effective number of points when they are weighted. Returns (h, details)."""
    xy = np.asarray(xy, dtype=float)
    w = np.ones(len(xy)) if w is None else np.asarray(w, dtype=float)
    spreads = []
    for k in range(2):
        sd = _wstd(xy[:, k], w)
        q75, q25 = np.percentile(xy[:, k], [75, 25])
        iqr = (q75 - q25) / 1.349
        spreads.append(min(sd, iqr) if iqr > 0 else sd)
    sigma = math.sqrt((spreads[0] ** 2 + spreads[1] ** 2) / 2.0)
    n = effective_n(w)
    return sigma * n ** (-1.0 / 6.0), {"sigma": sigma, "n": n}


def lcv_bandwidth(xy, w=None, h0=None, gen=None, max_points=2000, steps=31):
    """Likelihood cross-validation: the bandwidth that best predicts each point from all the others
    (leave-one-location-out log-likelihood, so repeated locations cannot pull h to zero). Returns
    (h, grid, scores)."""
    xy = np.asarray(xy, dtype=float)
    w = np.ones(len(xy)) if w is None else np.asarray(w, dtype=float)
    if h0 is None:
        h0 = silverman_bandwidth(xy, w)[0]
    if len(xy) > max_points:
        gen = gen if gen is not None else np.random.default_rng(12345)
        keep = gen.choice(len(xy), max_points, replace=False)
        xy, w = xy[keep], w[keep]
    grid = h0 * np.logspace(math.log10(0.08), math.log10(2.5), steps)
    from scipy.spatial import cKDTree
    tree = cKDTree(xy)
    i, j, d = _pairs_within(tree, 4.0 * grid[-1])
    keep = d > 1e-9 * max(h0, 1e-9)
    i, j, d = i[keep], j[keep], d[keep]
    total = w.sum()
    scores = np.empty(len(grid))
    for k, h in enumerate(grid):
        kv = w[j] * np.exp(-0.5 * (d / h) ** 2) / (2 * np.pi * h * h)
        f = np.bincount(i, weights=kv, minlength=len(xy)) / np.maximum(total - w, 1e-12)
        scores[k] = np.sum(w * np.log(np.maximum(f, 1e-300))) / w.sum()
    best = int(np.argmax(scores))
    return float(grid[best]), grid, scores


def _pairs_within(tree, r):
    m = tree.sparse_distance_matrix(tree, r, output_type="ndarray")
    sel = m["i"] != m["j"]
    return m["i"][sel], m["j"][sel], m["v"][sel]


def kde_grid(xy, w, h, extent, cell):
    """Gaussian kernel density on a grid: (density per square metre with row 0 at the top, x0, y1, cols, rows).

    Points are binned to the cells and the bins smoothed with the Gaussian (cut at 4 h), which keeps the total
    mass: the density integrates to the sum of the weights when the grid reaches 4 h beyond the points."""
    xy = np.asarray(xy, dtype=float)
    w = np.ones(len(xy)) if w is None else np.asarray(w, dtype=float)
    x0, y0, x1, y1 = extent
    cols = max(1, int(math.ceil((x1 - x0) / cell)))
    rows = max(1, int(math.ceil((y1 - y0) / cell)))
    x1, y1 = x0 + cols * cell, y0 + rows * cell
    c = np.clip(((xy[:, 0] - x0) / cell).astype(int), 0, cols - 1)
    r = np.clip(((y1 - xy[:, 1]) / cell).astype(int), 0, rows - 1)
    counts = np.zeros((rows, cols))
    np.add.at(counts, (r, c), w)
    sigma = h / cell
    from scipy import ndimage
    dens = ndimage.gaussian_filter(counts, sigma=sigma, mode="constant", truncate=4.0) if sigma > 0.05 else counts
    return dens / (cell * cell), x0, y1, cols, rows


def grid_for(extent, h, max_cells=1_500_000, target=400):
    """A cell size: about ``target`` cells along the longer side, at most h/3 (so the kernel is resolved),
    and not more than max_cells cells in all."""
    x0, y0, x1, y1 = extent
    long_side = max(x1 - x0, y1 - y0, 1e-9)
    cell = min(long_side / target, h / 3.0) if h > 0 else long_side / target
    cell = max(cell, math.sqrt((x1 - x0) * (y1 - y0) / max_cells))
    return _nice(cell)


def _nice(v):
    """Round a cell size to 1, 2 or 5 times a power of ten."""
    if v <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if v <= m * e * 1.0001:
            return m * e
    return 10 * e


def sample_grid(dens, x0, y1, cell, xy):
    cols = dens.shape[1]
    rows = dens.shape[0]
    c = np.clip(((xy[:, 0] - x0) / cell).astype(int), 0, cols - 1)
    r = np.clip(((y1 - xy[:, 1]) / cell).astype(int), 0, rows - 1)
    return dens[r, c]


def top_share(dens, inside, values_at_points, weights, share=0.10):
    """Share of the points (weights) lying in the densest ``share`` of the study area's cells."""
    cells = dens[inside]
    if not cells.size:
        return float("nan"), float("nan")
    thr = float(np.quantile(cells, 1.0 - share))
    w = np.asarray(weights, dtype=float)
    return float(w[values_at_points >= thr].sum() / max(w.sum(), 1e-300)), thr


# ---- central tendency and spread -----------------------------------------------------------------------
def mean_centre(xy, w=None):
    w = np.ones(len(xy)) if w is None else np.asarray(w, dtype=float)
    return np.average(np.asarray(xy, dtype=float), axis=0, weights=w)


def median_centre(xy, w=None, tol=1e-6, max_iter=1000):
    """The point with the smallest (weighted) sum of distances to all points: Weiszfeld's iteration with the
    Vardi-Zhang modification (correct when the answer is one of the points)."""
    xy = np.asarray(xy, dtype=float)
    w = np.ones(len(xy)) if w is None else np.asarray(w, dtype=float)
    y = np.average(xy, axis=0, weights=w)
    scale = max(float(np.ptp(xy, axis=0).max()), 1e-12)
    for _ in range(max_iter):
        d = np.hypot(*(xy - y).T)
        at = d < 1e-12 * scale
        dd = np.where(at, 1.0, d)
        ww = np.where(at, 0.0, w / dd)
        if ww.sum() == 0:
            return y
        t = (ww[:, None] * xy).sum(axis=0) / ww.sum()
        eta = w[at].sum()
        if eta > 0:
            r = np.hypot(*((ww[:, None] * (xy - y)).sum(axis=0)))
            gamma = min(1.0, eta / r) if r > 0 else 1.0
            nxt = (1 - gamma) * t + gamma * y
        else:
            nxt = t
        if np.hypot(*(nxt - y)) < tol * scale:
            return nxt
        y = nxt
    return y


def standard_distance(xy, w=None):
    """sqrt(mean squared distance to the mean centre): a circle of this radius holds about 63% of points
    spread as a round normal cloud (2 SD: 98%)."""
    xy = np.asarray(xy, dtype=float)
    w = np.ones(len(xy)) if w is None else np.asarray(w, dtype=float)
    c = np.average(xy, axis=0, weights=w)
    return math.sqrt(np.average(((xy - c) ** 2).sum(axis=1), weights=w))


def deviational_ellipse(xy, w=None):
    """Standard deviational ellipse: (centre, major semi-axis, minor semi-axis, azimuth of the major axis in
    degrees clockwise from north, 0-180).

    The axes are the principal directions of the (weighted) covariance; each semi-axis is sqrt(2) times the
    standard deviation along it (as ArcGIS Pro), so the 1 SD ellipse holds about 63% of a normal cloud,
    2 SD 98% and 3 SD 99.9%."""
    xy = np.asarray(xy, dtype=float)
    w = np.ones(len(xy)) if w is None else np.asarray(w, dtype=float)
    c = np.average(xy, axis=0, weights=w)
    d = xy - c
    cov = np.array([[np.average(d[:, 0] ** 2, weights=w), np.average(d[:, 0] * d[:, 1], weights=w)],
                    [0.0, np.average(d[:, 1] ** 2, weights=w)]])
    cov[1, 0] = cov[0, 1]
    vals, vecs = np.linalg.eigh(cov)
    major = vecs[:, 1]
    az = math.degrees(math.atan2(major[0], major[1])) % 180.0
    a = math.sqrt(2.0 * max(vals[1], 0.0))
    b = math.sqrt(2.0 * max(vals[0], 0.0))
    return c, a, b, az


def ellipse_ring(centre, a, b, azimuth, scale=1.0, n=96):
    """Closed ring of an ellipse with semi-axes a (along the azimuth) and b, times ``scale``."""
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    u = a * scale * np.cos(t)
    v = b * scale * np.sin(t)
    th = math.radians(azimuth)
    ux, uy = math.sin(th), math.cos(th)   # major axis direction
    vx, vy = uy, -ux                      # perpendicular
    x = centre[0] + u * ux + v * vx
    y = centre[1] + u * uy + v * vy
    ring = np.column_stack([x, y])
    return np.vstack([ring, ring[:1]])


def compass(dx, dy, lang="en"):
    """Eight-point compass word for a shift (dx east, dy north)."""
    names = {"en": ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"],
             "ar": ["الشمال", "الشمال الشرقي", "الشرق", "الجنوب الشرقي", "الجنوب", "الجنوب الغربي", "الغرب",
                    "الشمال الغربي"]}
    az = math.degrees(math.atan2(dx, dy)) % 360.0
    return names.get(lang, names["en"])[int((az + 22.5) // 45) % 8]
