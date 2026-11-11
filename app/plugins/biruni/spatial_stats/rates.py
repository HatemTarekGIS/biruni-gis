"""Rates for small numbers: raw rates of areas with few people jump around by chance (one case in a village
of 50 is a rate of 2,000 per 100,000), so the extremes of a rate map are mostly the smallest areas.
Empirical Bayes smoothing pulls each rate towards the overall (global EB) or the neighbourhood (local or
spatial EB) rate - the less people, the stronger the pull.

Marshall (1991) method-of-moments estimators, as in GeoDa and PySAL; exact Poisson confidence intervals
(Garwood). Neighbours: queen contiguity or k nearest centroids."""
import numpy as np


def raw_rates(events, pop):
    e = np.asarray(events, dtype=float)
    p = np.asarray(pop, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(p > 0, e / p, np.nan)


def poisson_ci(events, pop, level=0.95):
    """Exact (Garwood) confidence interval of the rate events / pop."""
    e = np.asarray(events, dtype=float)
    p = np.asarray(pop, dtype=float)
    a = (1 - level) / 2
    from scipy import stats
    lo = np.where(e > 0, stats.chi2.ppf(a, 2 * e) / 2, 0.0)
    hi = stats.chi2.ppf(1 - a, 2 * e + 2) / 2
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(p > 0, lo / p, np.nan), np.where(p > 0, hi / p, np.nan)


def _moments(e, p):
    """Marshall's prior mean b and variance a from events and populations (a >= 0)."""
    b = e.sum() / p.sum()
    r = e / p
    s2 = np.sum(p * (r - b) ** 2) / p.sum()
    a = s2 - b / p.mean()
    return b, max(a, 0.0)


def global_eb(events, pop):
    """Global Empirical Bayes rates, the weight each area's own rate keeps (0..1), the prior mean and variance."""
    e = np.asarray(events, dtype=float)
    p = np.asarray(pop, dtype=float)
    ok = p > 0
    b, a = _moments(e[ok], p[ok])
    w = np.full(len(p), np.nan)
    eb = np.full(len(p), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        w[ok] = a / (a + b / p[ok]) if a > 0 else 0.0
        eb[ok] = w[ok] * e[ok] / p[ok] + (1 - w[ok]) * b
    return eb, w, b, a


def local_eb(events, pop, neighbours):
    """Local (spatial) Empirical Bayes: the prior of each area is estimated from the area and its
    neighbours. neighbours: list of index arrays (without the area itself)."""
    e = np.asarray(events, dtype=float)
    p = np.asarray(pop, dtype=float)
    eb = np.full(len(p), np.nan)
    w = np.full(len(p), np.nan)
    for k in range(len(p)):
        if p[k] <= 0:
            continue
        idx = np.concatenate([[k], np.asarray(neighbours[k], dtype=int)])
        idx = idx[p[idx] > 0]
        if len(idx) < 2:
            eb[k], w[k] = e[k] / p[k], 1.0
            continue
        b, a = _moments(e[idx], p[idx])
        wk = a / (a + b / p[k]) if a > 0 else 0.0
        eb[k] = wk * e[k] / p[k] + (1 - wk) * b
        w[k] = wk
    return eb, w


def funnel_limits(rate, pop, level):
    """Exact Poisson control limits of a rate around ``rate`` for populations ``pop`` (a funnel plot)."""
    pop = np.asarray(pop, dtype=float)
    mu = rate * pop
    a = (1 - level) / 2
    from scipy import stats
    lo = np.where(mu > 0, stats.chi2.ppf(a, 2 * mu) / 2, 0.0) / pop
    hi = stats.chi2.ppf(1 - a, 2 * mu + 2) / 2 / pop
    return lo, hi


# ---- neighbours ----------------------------------------------------------------------------------------
def knn_neighbours(centres, k):
    centres = np.asarray(centres, dtype=float)
    k = max(1, min(k, len(centres) - 1))
    from scipy.spatial import cKDTree
    _, idx = cKDTree(centres).query(centres, k=k + 1)
    return [row[row != i][:k] for i, row in enumerate(idx)]


def band_neighbours(centres, distance):
    from scipy.spatial import cKDTree
    tree = cKDTree(np.asarray(centres, dtype=float))
    return [np.array([j for j in tree.query_ball_point(c, distance) if j != i], dtype=int)
            for i, c in enumerate(centres)]


def contiguity_neighbours(geometries, feedback=None):
    """Queen contiguity of QgsGeometry polygons (sharing an edge or a corner), with a spatial index."""
    from qgis.core import QgsFeature, QgsGeometry, QgsSpatialIndex

    index = QgsSpatialIndex()
    for k, g in enumerate(geometries):
        f = QgsFeature(k)
        f.setGeometry(g)
        index.addFeature(f)
    out = []
    for k, g in enumerate(geometries):
        if feedback is not None and feedback.isCanceled():
            break
        found = []
        if g is not None and not g.isEmpty():
            bb = g.boundingBox()
            tol = max(bb.width(), bb.height()) * 1e-7
            bb.grow(tol)
            eng = QgsGeometry.createGeometryEngine(g.constGet())
            eng.prepareGeometry()
            for j in index.intersects(bb):
                if j != k and geometries[j] is not None and eng.intersects(geometries[j].constGet()):
                    found.append(j)
        out.append(np.array(sorted(found), dtype=int))
    return out
