"""Spatial autocorrelation statistics, vectorised with numpy and scipy.sparse (no PySAL needed):
global Moran's I, local Moran (LISA), bivariate Moran, Getis-Ord Gi*, incremental Moran, the
co-location quotient and the Mann-Kendall trend test; plus the charts the reports show.

Formulas follow Cliff & Ord (1981) for the moments of Moran's I under randomisation, Anselin (1995)
for LISA with conditional permutation, Ord & Getis (1995) for Gi*, Leslie & Kronenfeld (2011) and
Wang et al. (2017) for the co-location quotient."""
import numpy as np

from . import weights as wts


# scipy is imported on first use: Biruni imports every tool module at start-up
def _sparse():
    from scipy import sparse

    return sparse


def _stats():
    from scipy import stats

    return stats

# LISA quadrants (numbers as in GeoDa / PySAL)
NS, HH, LH, LL, HL = 0, 1, 2, 3, 4
QUAD_COLOURS = {HH: "#D7191C", LH: "#ABD9E9", LL: "#2C7BB6", HL: "#FDAE61", NS: "#D9D9D9"}
QUAD_CODES = {HH: "HH", LH: "LH", LL: "LL", HL: "HL", NS: "NS"}
# Gi* confidence bins -3..3 (the classic red / blue scheme)
BIN_COLOURS = {-3: "#4575B5", -2: "#849EBA", -1: "#C0CCBE", 0: "#EDEDE4", 1: "#FAB984", 2: "#ED7551",
               3: "#D62F27"}


def zscores(y):
    y = np.asarray(y, dtype=float)
    return y - y.mean()


# ---- global Moran's I --------------------------------------------------------------------------------
def moran_moments(w, z):
    """Expected value and variance of I under randomisation (Cliff & Ord)."""
    n = z.size
    w = _sparse().csr_matrix(w)
    s0 = w.sum()
    s1 = 0.5 * (w + w.T).power(2).sum()
    s2 = float(((np.asarray(w.sum(1)).ravel() + np.asarray(w.sum(0)).ravel()) ** 2).sum())
    ei = -1.0 / (n - 1)
    m2 = (z * z).sum()
    b2 = n * (z ** 4).sum() / (m2 * m2)
    num = (n * ((n * n - 3 * n + 3) * s1 - n * s2 + 3 * s0 * s0)
           - b2 * ((n * n - n) * s1 - 2 * n * s2 + 6 * s0 * s0))
    vi = num / ((n - 1) * (n - 2) * (n - 3) * s0 * s0) - ei * ei
    return ei, max(vi, 1e-300)


def moran(y, w, permutations=0, rng=None):
    """Global Moran's I with w (row-standardised by the caller as wanted).
    -> dict I, EI, VI, z, p_norm (two-sided), p_sim, sims, lag (spatial lag of z), z_values."""
    z = zscores(y)
    n = z.size
    w = _sparse().csr_matrix(w, dtype=float)
    s0 = w.sum()
    m2 = (z * z).sum()
    lag = w @ z
    i = n / s0 * (z @ lag) / m2
    ei, vi = moran_moments(w, z)
    zs = (i - ei) / np.sqrt(vi)
    out = {"I": float(i), "EI": ei, "VI": vi, "z": float(zs), "p_norm": float(2 * _stats().norm.sf(abs(zs))),
           "lag": lag, "z_values": z, "n": n, "p_sim": None, "sims": None}
    if permutations:
        sims = np.empty(permutations)
        chunk = max(1, int(4e6 // max(n, 1)))
        for s in range(0, permutations, chunk):
            m = min(chunk, permutations - s)
            zp = np.stack([rng.permutation(z) for _ in range(m)], axis=1)  # n x m
            sims[s:s + m] = n / s0 * (zp * (w @ zp)).sum(0) / m2
        out["sims"] = sims
        from .common import perm_p

        out["p_sim"] = float(perm_p(i, sims))
    return out


def moran_bivariate(x, y, w):
    """Global bivariate Moran: how x here goes with the average y of the neighbours (standardised)."""
    zx = (x - x.mean()) / x.std()
    zy = (y - y.mean()) / y.std()
    return float(zx @ (_sparse().csr_matrix(w) @ zy) / zx.size)


# ---- local statistics with conditional permutation ---------------------------------------------------------
def _conditional(left, right, w, permutations, rng):
    """sim[i, p] = left[i] * sum_j w_ij right[random others]: i keeps its own value, its neighbours are
    drawn at random (without replacement) from the other n-1 features. The same random draws are reused
    for every feature, as PySAL does, which keeps it fast and reproducible."""
    w = _sparse().csr_matrix(w)
    n = w.shape[0]
    card = np.diff(w.indptr)
    kmax = int(card.max()) if n else 0
    sims = np.zeros((n, permutations))
    if kmax == 0 or permutations == 0:
        return sims
    kmax = min(kmax, n - 1)
    draws = np.stack([rng.choice(n - 1, kmax, replace=False) for _ in range(permutations)])  # P x kmax
    for k in np.unique(card):
        if k == 0:
            continue
        k = int(min(k, n - 1))
        rows = np.flatnonzero(card == k)
        wk = np.stack([w.data[w.indptr[r]:w.indptr[r] + k] for r in rows])  # m x k
        d = draws[:, :k]
        step = max(1, int(3e6 // (permutations * k)))
        for s in range(0, rows.size, step):
            r = rows[s:s + step]
            idx = d[None, :, :] + (d[None, :, :] >= r[:, None, None])  # skip i itself
            sims[r] = left[r, None] * np.einsum("mpk,mk->mp", right[idx], wk[s:s + step])
    return sims


def _folded_p(obs, sims):
    """PySAL's pseudo p-value: the share of permutations at least as extreme, on the observed side."""
    p_count = sims.shape[1]
    larger = (sims >= obs[:, None]).sum(1)
    low = p_count - larger
    larger = np.where(low < larger, low, larger)
    return (larger + 1.0) / (p_count + 1.0)


def local_moran(y, w, permutations=999, rng=None, alpha=0.05, use_fdr=True):
    """LISA with row-standardised w. -> dict Is, z_sim, p, q (quadrant), sig (bool), cluster (q or NS)."""
    from .common import fdr

    z = zscores(y)
    n = z.size
    w = wts.row_standardise(w)
    den = (z * z).sum()
    lag = w @ z
    scale = (n - 1) / den
    li = scale * z * lag
    sims = scale * _conditional(z, z, w, permutations, rng)
    return _local_result(li, sims, z, lag, w, alpha, use_fdr, fdr)


def local_bivariate(x, y, w, permutations=999, rng=None, alpha=0.05, use_fdr=True):
    """Bivariate LISA: x here (standardised) times the average standardised y of the neighbours."""
    from .common import fdr

    zx = (x - x.mean()) / x.std()
    zy = (y - y.mean()) / y.std()
    w = wts.row_standardise(w)
    lag = w @ zy
    li = zx * lag
    sims = _conditional(zx, zy, w, permutations, rng)
    return _local_result(li, sims, zx, lag, w, alpha, use_fdr, fdr)


def _local_result(li, sims, z, lag, w, alpha, use_fdr, fdr):
    card = wts.cardinality(w)
    p = _folded_p(li, sims) if sims.shape[1] else np.ones(li.size)
    p = np.where(card > 0, p, 1.0)
    sd = sims.std(1)
    zsim = np.divide(li - sims.mean(1), sd, out=np.zeros_like(li), where=sd > 0)
    q = quadrant(z, lag)
    sig = fdr(p, alpha) if use_fdr else (p <= alpha)
    sig &= card > 0
    return {"Is": li, "z_sim": zsim, "p": p, "q": q, "sig": sig, "cluster": np.where(sig, q, NS), "lag": lag,
            "z": z}


def quadrant(z, lag):
    """1 HH (high among high), 2 LH, 3 LL, 4 HL; the Moran scatterplot quarters."""
    return np.where(z > 0, np.where(lag > 0, HH, HL), np.where(lag > 0, LH, LL))


# ---- Getis-Ord Gi* --------------------------------------------------------------------------------------
def gi_star(y, w):
    """Gi* z-scores (self included). w: binary or distance weights without the diagonal; the diagonal
    is set to 1 here. -> z, p (two-sided normal)."""
    x = np.asarray(y, dtype=float)
    n = x.size
    ws = wts.with_self(_sparse().csr_matrix(w, dtype=float) - _sparse().diags(_sparse().csr_matrix(w).diagonal()))
    xbar = x.mean()
    s = np.sqrt((x * x).mean() - xbar * xbar)
    wsum = np.asarray(ws.sum(1)).ravel()
    w2 = np.asarray(ws.multiply(ws).sum(1)).ravel()
    num = ws @ x - xbar * wsum
    den = s * np.sqrt(np.maximum(n * w2 - wsum * wsum, 0) / (n - 1))
    z = np.divide(num, den, out=np.zeros(n), where=den > 0)
    return z, 2 * _stats().norm.sf(np.abs(z))


def confidence_bins(z, p, use_fdr=True):
    """-3..3: 3 = hot spot with 99% confidence, 2 = 95%, 1 = 90%, negative = cold, 0 = not significant.
    With FDR the cut-offs are tightened (Benjamini-Hochberg) for the many places tested at once."""
    from .common import fdr

    b = np.zeros(z.size, dtype=int)
    for level, alpha in ((1, 0.10), (2, 0.05), (3, 0.01)):
        sig = fdr(p, alpha) if use_fdr else (p <= alpha)
        b[sig] = level
    return b * np.sign(z).astype(int)


# ---- incremental spatial autocorrelation ---------------------------------------------------------------
def incremental(xy, y, distances, feedback=None):
    """Moran's I (row-standardised distance band) at each distance -> rows (d, I, z, p, mean neighbours)."""
    rows = []
    for d in distances:
        if feedback is not None and feedback.isCanceled():
            break
        w = wts.distance_band(xy, d)
        if w.nnz == 0:
            continue
        r = moran(y, wts.row_standardise(w))
        rows.append((float(d), r["I"], r["z"], r["p_norm"], w.nnz / len(y)))
    return rows


def peaks(zs, significant=1.96):
    """Indexes of the local maxima of the z-score curve that are significant: [first peak, ...]."""
    zs = np.asarray(zs, dtype=float)
    out = []
    for k in range(zs.size):
        left = zs[k - 1] if k > 0 else -np.inf
        right = zs[k + 1] if k + 1 < zs.size else -np.inf
        if zs[k] > significant and zs[k] >= left and zs[k] > right:
            out.append(k)
    return out


def default_bands(xy, count=10):
    """Start: the distance that gives every feature a neighbour; step: so the last band reaches about a
    quarter of the study area's diagonal, kept small enough that the matrices stay manageable."""
    xy = np.asarray(xy, dtype=float)
    start = wts.min_threshold(xy)
    diag = float(np.hypot(np.ptp(xy[:, 0]), np.ptp(xy[:, 1])))
    end = max(diag / 4.0, start * 2.0)
    step = (end - start) / max(count - 1, 1)
    if step <= 0:
        step = max(start, 1.0) * 0.25
    return start, step


def cap_bands(xy, distances, max_pairs=4e6):
    """Drop the bands whose neighbour pairs would make the matrix too big."""
    counts = wts.pair_count(xy, distances)
    return [d for d, c in zip(distances, counts) if c <= max_pairs] or list(distances[:1])


# ---- co-location quotient -------------------------------------------------------------------------------
def colocation(xy, is_a, is_b, k=8, permutations=999, rng=None):
    """CLQ A->B with the k nearest neighbours (equal weights): the share of A's neighbours that are B,
    divided by the share of B among all other points. 1 = as expected, >1 = B is found next to A more
    often than chance, <1 = B avoids A. Global p by random relabelling; local p exact (hypergeometric:
    k neighbours drawn without replacement from the other N-1 points)."""
    from .common import perm_p

    xy = np.asarray(xy, dtype=float)
    n = len(xy)
    k = int(min(k, n - 1))
    w = wts.knn(xy, k)
    nb = w.indices.reshape(n, k)
    is_a = np.asarray(is_a, dtype=bool)
    is_b = np.asarray(is_b, dtype=bool)
    same = bool(np.array_equal(is_a, is_b))
    na, nbn = int(is_a.sum()), int(is_b.sum())
    nb_eff = nbn - 1 if same else nbn
    expected = nb_eff / (n - 1)
    share = is_b[nb].mean(1)  # share of each point's neighbours that are B
    clq = share[is_a].mean() / expected if expected > 0 else np.nan
    local = share / expected if expected > 0 else np.full(n, np.nan)
    # exact local p-values: neighbours that are B among k drawn from N-1 others (N'_B of them B)
    count_b = np.rint(share * k).astype(int)
    p_hi = _stats().hypergeom.sf(count_b - 1, n - 1, nb_eff, k)
    p_lo = _stats().hypergeom.cdf(count_b, n - 1, nb_eff, k)
    p_local = np.minimum(1.0, 2 * np.minimum(p_hi, p_lo))
    sims = np.empty(permutations)
    labels = np.where(is_a & is_b, 3, np.where(is_a, 1, np.where(is_b, 2, 0)))
    chunk = max(1, int(2e6 // max(n * k, 1)))
    for s in range(0, permutations, chunk):
        m = min(chunk, permutations - s)
        lp = np.stack([rng.permutation(labels) for _ in range(m)])  # m x n
        ap = (lp == 1) | (lp == 3)
        bp = (lp == 2) | (lp == 3)
        sh = bp[:, nb].mean(2)  # m x n
        sims[s:s + m] = (sh * ap).sum(1) / max(na, 1) / expected
    return {"clq": float(clq), "p": float(perm_p(clq, sims)), "sims": sims, "local": local, "p_local": p_local,
            "count_b": count_b, "k": k, "n_a": na, "n_b": nbn, "expected": expected}


# ---- Mann-Kendall trend test -----------------------------------------------------------------------------------
def mann_kendall(series):
    """Mann-Kendall for each row of a 2-D array (rows = places, columns = time). -> S, z, p (two-sided).
    Ties are corrected for in the variance."""
    x = np.atleast_2d(np.asarray(series, dtype=float))
    m, t = x.shape
    i, j = np.triu_indices(t, 1)
    s = np.sign(x[:, j] - x[:, i]).sum(1)
    var = np.full(m, t * (t - 1) * (2 * t + 5) / 18.0)
    for r in range(m):
        _, c = np.unique(x[r], return_counts=True)
        c = c[c > 1]
        if c.size:
            var[r] -= (c * (c - 1) * (2 * c + 5)).sum() / 18.0
    sd = np.sqrt(np.maximum(var, 1e-12))
    z = np.where(s > 0, (s - 1) / sd, np.where(s < 0, (s + 1) / sd, 0.0))
    return s, z, 2 * _stats().norm.sf(np.abs(z))


# ---- charts ---------------------------------------------------------------------------------------------
def _t(text):
    """Chart text (common.chart_text knows which matplotlib lays out Arabic itself)."""
    from .common import chart_text

    return chart_text(text)


def scatter_png(z, lag, slope, cluster, labels, title):
    """Moran scatterplot: value (standardised) against the neighbours' average; slope = Moran's I."""
    from .common import figure, png

    fig = figure(6.0, 5.0)
    ax = fig.add_subplot(111)
    sd = z.std() or 1.0
    xs, ys = z / sd, lag / sd
    colours = [QUAD_COLOURS[int(c)] for c in cluster]
    ax.scatter(xs, ys, s=9, c=colours, alpha=0.75, linewidths=0)
    lim = np.array([xs.min(), xs.max()])
    ax.plot(lim, slope * lim, color="#1d2329", lw=1.4)
    ax.axhline(0, color="#888", lw=0.7)
    ax.axvline(0, color="#888", lw=0.7)
    ax.set_xlabel(_t(labels["x"]))
    ax.set_ylabel(_t(labels["y"]))
    ax.set_title(_t(title), fontsize=11)
    for txt, (hx, hy) in ((labels["HH"], (0.97, 0.95)), (labels["LL"], (0.03, 0.04)),
                         (labels["LH"], (0.03, 0.95)), (labels["HL"], (0.97, 0.04))):
        ax.text(hx, hy, _t(txt), transform=ax.transAxes, ha="right" if hx > 0.5 else "left",
                va="top" if hy > 0.5 else "bottom", fontsize=8, color="#555")
    ax.grid(alpha=0.25)
    return png(fig)


def map_png(shapes, colours, legend, title, points=None):
    """A small map: shapes = list of rings (arrays) per feature or None; points (n,2) for point layers.
    colours: one per feature; legend: [(colour, label)]."""
    from matplotlib.collections import PolyCollection
    from matplotlib.patches import Patch

    from .common import figure, png

    fig = figure(6.4, 5.6)
    ax = fig.add_subplot(111)
    if points is not None:
        order = np.argsort([c == "#D9D9D9" or c == "#EDEDE4" for c in colours])[::-1]  # coloured on top
        ax.scatter(points[order, 0], points[order, 1], s=7, c=[colours[i] for i in order], linewidths=0)
    else:
        polys, cols = [], []
        for rings, c in zip(shapes, colours):
            for r in rings or ():
                polys.append(r)
                cols.append(c)
        ax.add_collection(PolyCollection(polys, facecolors=cols, edgecolors="#ffffff", linewidths=0.2))
        ax.autoscale_view()
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(_t(title), fontsize=11)
    ax.legend(handles=[Patch(color=c, label=_t(t)) for c, t in legend], loc="upper left",
              bbox_to_anchor=(1.0, 1.0), fontsize=8, frameon=False)
    return png(fig)


def line_png(x, ys, labels, title, xlabel, ylabel, marks=(), hline=None, logx=False, y2=None):
    """Line chart(s): ys = list of series, labels = their names; marks = x positions to highlight;
    y2 = (series, label) drawn on a second axis."""
    from .common import figure, png

    fig = figure(6.4, 4.0)
    ax = fig.add_subplot(111)
    palette = ["#12A594", "#C0392B", "#2C7BB6"]
    for k, (y, lab) in enumerate(zip(ys, labels)):
        ax.plot(x, y, marker="o", ms=4, color=palette[k % 3], label=_t(lab))
    for m in marks:
        ax.axvline(m, color="#C98A12", lw=1.2, ls="--")
    if hline is not None:
        ax.axhline(hline, color="#888", lw=0.8, ls=":")
    if logx:
        from matplotlib.ticker import NullLocator

        ax.set_xscale("log")
        ax.xaxis.set_minor_locator(NullLocator())
        ax.set_xticks(list(x))
        ax.set_xticklabels([f"{v:,.0f}" for v in x], fontsize=8)
    ax.set_xlabel(_t(xlabel))
    ax.set_ylabel(_t(ylabel))
    ax.set_title(_t(title), fontsize=11)
    ax.grid(alpha=0.25)
    if y2 is not None:
        ax2 = ax.twinx()
        ax2.plot(x, y2[0], marker="s", ms=4, color="#7F8C8D", ls="--", label=_t(y2[1]))
        ax2.set_ylabel(_t(y2[1]))
        ax2.set_ylim(bottom=0, top=max(max(y2[0]) * 1.15, 1.0))
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, fontsize=8, frameon=False)
    elif len(ys) > 1 or labels[0]:
        ax.legend(fontsize=8, frameon=False)
    return png(fig)


def hist_png(sims, observed, title, xlabel):
    """Permutation distribution with the observed value marked."""
    from .common import figure, png

    fig = figure(6.0, 3.6)
    ax = fig.add_subplot(111)
    ax.hist(sims, bins=40, color="#B8D8D3", edgecolor="#ffffff")
    ax.axvline(observed, color="#C0392B", lw=2)
    ax.set_xlabel(_t(xlabel))
    ax.set_title(_t(title), fontsize=11)
    ax.grid(alpha=0.25)
    return png(fig)


def bars_png(labels, values, colours, title, ylabel, dates=None):
    from .common import figure, png

    fig = figure(6.6, 3.8)
    ax = fig.add_subplot(111)
    xs = np.arange(len(values))
    ax.bar(xs, values, color=colours)
    step = max(1, len(labels) // 12)
    ax.set_xticks(xs[::step])
    ax.set_xticklabels([_t(str(t)) for t in labels[::step]], rotation=30 if dates else 0,
                       ha="right" if dates else "center", fontsize=8)
    ax.set_ylabel(_t(ylabel))
    ax.set_title(_t(title), fontsize=11)
    ax.grid(alpha=0.25, axis="y")
    return png(fig)
