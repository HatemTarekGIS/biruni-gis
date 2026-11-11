"""Point patterns: is a set of points clustered, random or evenly spread, and at what distances?

Pure numpy/scipy (no QGIS): the study area is a ``Region`` (polygons with holes, in metres), the points
an (n, 2) array in the same metric system.

- Clark-Evans nearest neighbour ratio, analytic (with or without Donnelly's perimeter correction) and by
  Monte Carlo simulation of complete spatial randomness (CSR) inside the actual study area;
- Ripley's K and L(d) - d at many distances with simulation envelopes, Ripley's isotropic or the border
  edge correction, a global deviation test, and the cross-K of two categories (random labelling);
- DBSCAN density clusters with eps suggested from the k-distance elbow."""
import math

import numpy as np


# ---- the study area ------------------------------------------------------------------------------------
class Region:
    """Polygons with holes in a metric CRS: area, inside test, random points, distance to the boundary and
    the share of a circle that lies inside (for edge corrections)."""

    def __init__(self, polygons):
        """polygons: [[exterior ring, hole ring, ...], ...]; a ring is an (m, 2) array (closed or not)."""
        from matplotlib.path import Path

        self.polygons = []
        segs = []
        area = perim = 0.0
        for poly in polygons:
            rings = [np.asarray(r, dtype=float)[:, :2] for r in poly if len(r) >= 3]
            if not rings:
                continue
            paths = [Path(r) for r in rings]
            self.polygons.append(paths)
            for k, r in enumerate(rings):
                a = abs(_ring_area(r))
                area += a if k == 0 else -a
                closed = r if np.allclose(r[0], r[-1]) else np.vstack([r, r[:1]])
                s = np.hstack([closed[:-1], closed[1:]])
                segs.append(s)
                perim += float(np.hypot(s[:, 2] - s[:, 0], s[:, 3] - s[:, 1]).sum())
        if not self.polygons:
            raise ValueError("empty region")
        self.area = area
        self.perimeter = perim
        self.segments = np.vstack(segs)
        allpts = np.vstack([self.segments[:, :2], self.segments[:, 2:]])
        self.bbox = (allpts[:, 0].min(), allpts[:, 1].min(), allpts[:, 0].max(), allpts[:, 1].max())

    @classmethod
    def rectangle(cls, xmin, ymin, xmax, ymax):
        return cls([[np.array([[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax]])]])

    def contains(self, xy):
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        inside = np.zeros(len(xy), dtype=bool)
        for paths in self.polygons:
            ext = paths[0]
            b = ext.get_extents()
            cand = ((xy[:, 0] >= b.x0) & (xy[:, 0] <= b.x1) & (xy[:, 1] >= b.y0) & (xy[:, 1] <= b.y1)) & ~inside
            if not cand.any():
                continue
            idx = np.nonzero(cand)[0]
            ok = ext.contains_points(xy[idx])
            for hole in paths[1:]:
                ok &= ~hole.contains_points(xy[idx])
            inside[idx[ok]] = True
        return inside

    def random(self, n, gen):
        """n points of complete spatial randomness (uniform) inside the region (rejection from the box)."""
        x0, y0, x1, y1 = self.bbox
        frac = max(self.area / max((x1 - x0) * (y1 - y0), 1e-12), 0.01)
        out = []
        have = 0
        while have < n:
            m = int((n - have) / frac * 1.2) + 16
            p = np.column_stack([gen.uniform(x0, x1, m), gen.uniform(y0, y1, m)])
            p = p[self.contains(p)]
            out.append(p)
            have += len(p)
        return np.vstack(out)[:n]

    def boundary_distance(self, xy, chunk=200000):
        """Distance from each point to the nearest edge of the region."""
        xy = np.asarray(xy, dtype=float)
        s = self.segments
        ax, ay, bx, by = s[:, 0], s[:, 1], s[:, 2], s[:, 3]
        dx, dy = bx - ax, by - ay
        ll = np.maximum(dx * dx + dy * dy, 1e-18)
        out = np.empty(len(xy))
        step = max(1, chunk // max(len(s), 1))
        for k in range(0, len(xy), step):
            p = xy[k:k + step]
            t = np.clip(((p[:, :1] - ax) * dx + (p[:, 1:] - ay) * dy) / ll, 0.0, 1.0)
            qx = ax + t * dx - p[:, :1]
            qy = ay + t * dy - p[:, 1:]
            out[k:k + step] = np.sqrt((qx * qx + qy * qy).min(axis=1))
        return out

    def circle_inside(self, xy, radii, bdist=None, n_angles=64):
        """(n, len(radii)) share of the circle of each radius around each point that lies inside the region
        (1 where the circle does not reach the boundary)."""
        xy = np.asarray(xy, dtype=float)
        radii = np.asarray(radii, dtype=float)
        if bdist is None:
            bdist = self.boundary_distance(xy)
        frac = np.ones((len(xy), len(radii)))
        ang = (np.arange(n_angles) + 0.5) * 2 * np.pi / n_angles
        ca, sa = np.cos(ang), np.sin(ang)
        for k, r in enumerate(radii):
            need = np.nonzero(bdist < r)[0]
            if r <= 0 or not len(need):
                continue
            px = (xy[need, 0][:, None] + r * ca).ravel()
            py = (xy[need, 1][:, None] + r * sa).ravel()
            ins = self.contains(np.column_stack([px, py])).reshape(len(need), n_angles)
            frac[need, k] = ins.mean(axis=1)
        return frac


def _ring_area(r):
    x, y = r[:, 0], r[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def duplicates(xy):
    """How many points repeat a location already taken by another point."""
    if len(xy) == 0:
        return 0
    return int(len(xy) - len(np.unique(np.round(np.asarray(xy), 6), axis=0)))


def fmt_distance(d, lang="en"):
    """200 m, 1.4 km."""
    if d >= 1000:
        v = f"{d / 1000:.2g}" if d < 10000 else f"{d / 1000:.0f}"
        return v + (" كم" if lang == "ar" else " km")
    v = f"{d:.2g}" if d < 100 else f"{round(d, -1):.0f}"
    return v + (" م" if lang == "ar" else " m")


# ---- Clark-Evans ---------------------------------------------------------------------------------------
def nn_distances(xy):
    from scipy.spatial import cKDTree
    d, _ = cKDTree(xy).query(xy, k=2)
    return d[:, 1]


def clark_evans(xy, area, perimeter=None, correction="none"):
    """Nearest neighbour ratio R = observed mean NN distance / the mean expected under CSR.

    correction "donnelly": Donnelly's (1978) expectation and variance for a bounded study area (they need the
    perimeter), else Clark & Evans' (1954) infinite-plane values. Returns a dict."""
    n = len(xy)
    nn = nn_distances(xy)
    obs = float(nn.mean())
    if correction == "donnelly" and perimeter:
        exp = 0.5 * math.sqrt(area / n) + (0.0514 + 0.041 / math.sqrt(n)) * perimeter / n
        se = math.sqrt(0.0703 * area / n ** 2 + 0.037 * perimeter * math.sqrt(area / n ** 5))
    else:
        lam = n / area
        exp = 0.5 / math.sqrt(lam)
        se = 0.26136 / math.sqrt(n * lam)
    z = (obs - exp) / se
    from scipy import stats
    return {"n": n, "observed": obs, "expected": exp, "R": obs / exp, "z": z, "p": float(2 * stats.norm.sf(abs(z))),
            "nn": nn}


def nn_monte_carlo(observed, n, region, sims, gen):
    """Mean NN distance of ``sims`` CSR patterns of n points in the region; the simulated mean, a two-sided
    rank p-value and the ratio observed / simulated mean (an R whose edge effects cancel)."""
    sim = np.array([nn_distances(region.random(n, gen)).mean() for _ in range(sims)])
    lo = (np.sum(sim <= observed) + 1.0) / (sims + 1.0)
    hi = (np.sum(sim >= observed) + 1.0) / (sims + 1.0)
    return {"sims": sim, "mean": float(sim.mean()), "R": observed / float(sim.mean()),
            "p": float(min(1.0, 2 * min(lo, hi))), "low": float(np.percentile(sim, 2.5)),
            "high": float(np.percentile(sim, 97.5))}


# ---- Ripley's K and L ----------------------------------------------------------------------------------
def default_distances(region, steps=30, dmax=0.0):
    """Distances 0 < d <= dmax; dmax defaults to a quarter of the shorter side of the region's box (beyond
    that the edge corrections dominate the estimate)."""
    x0, y0, x1, y1 = region.bbox
    if not dmax or dmax <= 0:
        dmax = 0.25 * min(x1 - x0, y1 - y0)
    return np.linspace(dmax / steps, dmax, steps)


class _Pairs:
    """Ordered pairs (i, j) within dmax with their distance and edge-correction weight."""

    def __init__(self, xy, region, dmax, correction, n_grid=24):
        self.n = len(xy)
        from scipy.spatial import cKDTree
        tree = cKDTree(xy)
        p = tree.query_pairs(dmax, output_type="ndarray")
        i = np.concatenate([p[:, 0], p[:, 1]]) if len(p) else np.zeros(0, int)
        j = np.concatenate([p[:, 1], p[:, 0]]) if len(p) else np.zeros(0, int)
        d = np.hypot(*(xy[i] - xy[j]).T) if len(i) else np.zeros(0)
        self.i, self.j, self.d = i, j, d
        self.bdist = region.boundary_distance(xy)
        if correction == "isotropic":
            grid = np.linspace(0, dmax, n_grid + 1)[1:]
            frac = np.clip(region.circle_inside(xy, grid, self.bdist), 0.1, 1.0)
            # linear interpolation of the inside share at each pair's distance, from the centre point i
            pos = np.clip(d / dmax * n_grid - 1.0, 0.0, n_grid - 1.0)
            k0 = np.floor(pos).astype(int)
            k1 = np.minimum(k0 + 1, n_grid - 1)
            t = pos - k0
            e = frac[i, k0] * (1 - t) + frac[i, k1] * t
            self.w = 1.0 / e
        else:
            self.w = np.ones(len(i))


def k_function(xy, region, distances, correction="isotropic", pairs=None):
    """Ripley's K(d) at the distances (edge corrections: "isotropic" = Ripley's, "border" = reduced
    sample, "none")."""
    n = len(xy)
    dist = np.asarray(distances, dtype=float)
    pr = pairs or _Pairs(xy, region, dist[-1], correction)
    lam = n / region.area
    if correction == "border":
        out = np.empty(len(dist))
        for k, r in enumerate(dist):
            centre = pr.bdist >= r
            m = centre.sum()
            if m == 0:
                out[k] = np.nan
                continue
            cnt = np.sum(centre[pr.i] & (pr.d <= r))
            out[k] = cnt / (lam * m)
        return out
    order = np.argsort(pr.d)
    cw = np.concatenate([[0.0], np.cumsum(pr.w[order])])
    idx = np.searchsorted(pr.d[order], dist, side="right")
    return region.area * cw[idx] / (n * (n - 1))


def l_minus_d(k, distances):
    return np.sqrt(np.maximum(k, 0) / np.pi) - np.asarray(distances)


def ripley(xy, region, distances, correction="isotropic", sims=99, gen=None, max_points=1500):
    """L(d) - d of the points with the pointwise envelope of ``sims`` CSR patterns in the region.

    Patterns larger than max_points are randomly thinned first: independent thinning leaves K unchanged, so
    the curve keeps its expected shape while the simulations stay fast. Returns a dict with the curve, the
    envelope, the global deviation test p-value (maximum absolute deviation, Loosmore & Ford 2006) and the
    distance ranges where the curve leaves the envelope."""
    gen = gen if gen is not None else np.random.default_rng(12345)
    xy = np.asarray(xy, dtype=float)
    thinned = len(xy) > max_points
    if thinned:
        xy = xy[gen.choice(len(xy), max_points, replace=False)]
    n = len(xy)
    dist = np.asarray(distances, dtype=float)
    obs = l_minus_d(k_function(xy, region, dist, correction), dist)
    sim = np.empty((sims, len(dist)))
    for s in range(sims):
        sim[s] = l_minus_d(k_function(region.random(n, gen), region, dist, correction), dist)
    return _envelope(obs, sim, dist, n, thinned)


def cross_k(xy, is_a, region, distances, correction="isotropic", sims=99, gen=None, max_points=3000):
    """Cross L_AB(d) - d of category A towards category B with a random labelling envelope: the labels are
    shuffled among the same locations, so the test asks whether B lies nearer to A than the pattern itself
    explains."""
    gen = gen if gen is not None else np.random.default_rng(12345)
    xy = np.asarray(xy, dtype=float)
    is_a = np.asarray(is_a, dtype=bool)
    thinned = len(xy) > max_points
    if thinned:
        keep = gen.choice(len(xy), max_points, replace=False)
        xy, is_a = xy[keep], is_a[keep]
    dist = np.asarray(distances, dtype=float)
    pr = _Pairs(xy, region, dist[-1], correction)
    order = np.argsort(pr.d)
    i, j, d, w = pr.i[order], pr.j[order], pr.d[order], pr.w[order]
    na = int(is_a.sum())
    nb = len(xy) - na
    idx = np.searchsorted(d, dist, side="right")

    def lab(lbl):
        sel = lbl[i] & ~lbl[j]
        if correction == "border":
            out = np.empty(len(dist))
            for k, r in enumerate(dist):
                centre = lbl & (pr.bdist >= r)
                m = centre.sum()
                out[k] = np.nan if m == 0 else np.sum(sel & centre[i] & (d <= r)) * region.area / (m * nb)
            return l_minus_d(out, dist)
        cw = np.concatenate([[0.0], np.cumsum(w * sel)])
        return l_minus_d(region.area * cw[idx] / (na * nb), dist)

    obs = lab(is_a)
    sim = np.empty((sims, len(dist)))
    for s in range(sims):
        sim[s] = lab(gen.permutation(is_a))
    return _envelope(obs, sim, dist, len(xy), thinned)


def _envelope(obs, sim, dist, n, thinned):
    lo = np.nanmin(sim, axis=0)
    hi = np.nanmax(sim, axis=0)
    mean = np.nanmean(sim, axis=0)
    dev_obs = np.nanmax(np.abs(obs - mean))
    dev_sim = np.nanmax(np.abs(sim - mean), axis=1)
    p = (np.sum(dev_sim >= dev_obs) + 1.0) / (len(sim) + 1.0)
    state = np.where(obs > hi, 1, np.where(obs < lo, -1, 0))
    return {"d": dist, "L": obs, "low": lo, "high": hi, "mean": mean, "p": float(p), "state": state,
            "runs": runs(state, dist), "n": n, "thinned": thinned, "sims": len(sim)}


def runs(state, dist):
    """[(state, from distance, to distance)] for consecutive distances in the same state."""
    out = []
    for k, s in enumerate(state):
        if out and out[-1][0] == s:
            out[-1][2] = dist[k]
        else:
            out.append([int(s), dist[k], dist[k]])
    return [tuple(r) for r in out]


# ---- DBSCAN --------------------------------------------------------------------------------------------
def k_distance(xy, k):
    """Distance from each point to its k-th nearest other point."""
    k = max(1, min(k, len(xy) - 1))
    from scipy.spatial import cKDTree
    d, _ = cKDTree(xy).query(xy, k=k + 1)
    return d[:, k]


def suggest_eps(xy, min_points):
    """eps at the elbow of the sorted k-distance curve (k = min_points - 1): the point of the curve furthest
    below the chord from its start to its end (the 'kneedle' rule). Returns (eps, sorted k-distances)."""
    kd = np.sort(k_distance(xy, max(1, min_points - 1)))
    n = len(kd)
    if n < 3 or kd[-1] <= kd[0]:
        return float(kd[-1]) if n else 0.0, kd
    x = np.linspace(0.0, 1.0, n)
    y = (kd - kd[0]) / (kd[-1] - kd[0])
    gap = x - y
    k = int(np.argmax(gap))
    return float(kd[k]), kd


def dbscan(xy, eps, min_points):
    """Cluster labels 1..k by size (largest first), 0 for noise, and the core-point flags.

    A core point has at least min_points points (itself included) within eps; core points within eps of each
    other share a cluster; a border point joins the cluster of its nearest core point."""
    n = len(xy)
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    from scipy.spatial import cKDTree
    pairs = cKDTree(xy).query_pairs(eps, output_type="ndarray")
    deg = np.bincount(pairs.ravel(), minlength=n) if len(pairs) else np.zeros(n, int)
    core = deg + 1 >= min_points
    labels = np.zeros(n, dtype=int)
    if not core.any():
        return labels, core
    cc = pairs[core[pairs[:, 0]] & core[pairs[:, 1]]] if len(pairs) else np.zeros((0, 2), int)
    g = coo_matrix((np.ones(len(cc)), (cc[:, 0], cc[:, 1])), shape=(n, n))
    _, comp = connected_components(g, directed=False)
    core_idx = np.nonzero(core)[0]
    uniq, inv = np.unique(comp[core_idx], return_inverse=True)
    labels[core_idx] = inv + 1
    # border points: nearest core neighbour
    if len(pairs):
        a = np.concatenate([pairs[:, 0], pairs[:, 1]])
        b = np.concatenate([pairs[:, 1], pairs[:, 0]])
        sel = ~core[a] & core[b]
        if sel.any():
            a, b = a[sel], b[sel]
            d = np.hypot(*(xy[a] - xy[b]).T)
            order = np.lexsort((d, a))
            a, b = a[order], b[order]
            first = np.concatenate([[True], a[1:] != a[:-1]])
            labels[a[first]] = labels[b[first]]
    # renumber by size
    ids, counts = np.unique(labels[labels > 0], return_counts=True)
    remap = np.zeros(labels.max() + 1, dtype=int)
    remap[ids[np.argsort(-counts, kind="stable")]] = np.arange(1, len(ids) + 1)
    return remap[labels], core
