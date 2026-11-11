"""Ordinary least squares with spatial diagnostics (the numbers behind biruni:ssols).

Everything here is plain numpy/scipy so it can be tested without QGIS (except read_features, which reads a
Processing source). Definitions follow the usual textbook / ArcGIS "OLS" conventions:

* coefficients, standard errors, t and two-sided p from the t distribution with n - k degrees of freedom;
* robust standard errors: White's HC1 (sandwich estimator scaled by n / (n - k));
* AICc = n ln(RSS / n) + n ln(2 pi) + n (n + k) / (n - 2 - k) - the same formula GWR uses with k = tr(S)
  (Fotheringham, Brunsdon & Charlton 2002, eq. 2.33), so the two models can be compared directly;
* VIF_j = 1 / (1 - R_j^2), R_j^2 from regressing explanatory j on the others;
* Jarque-Bera on the residuals (normality), chi-square with 2 df;
* Koenker-Bassett (studentised Breusch-Pagan) = n R^2 of e^2 on X, chi-square with k - 1 df: a significant
  value means the spread of the residuals changes with the explanatory values - heteroscedasticity, often a
  sign that the relationships are not the same everywhere (non-stationarity);
* Moran's I of the residuals with row-standardised k-nearest-neighbour or distance-band weights and a
  permutation p-value: clustered residuals mean a missing variable or relationships that vary in space.
"""
import math

import numpy as np

from .common import chart_text, figure, perm_p, png, rng, say

VIF_LIMIT = 7.5


# ---------------------------------------------------------------- model ---

def aicc(rss, n, k):
    """Corrected Akaike information criterion; inf when the model uses up the data (n - 2 - k <= 0)."""
    if n - 2 - k <= 0 or rss <= 0:
        return math.inf
    return n * math.log(rss / n) + n * math.log(2 * math.pi) + n * (n + k) / (n - 2 - k)


def ols(y, X, names=None, intercept=True):
    """y (n,), X (n, m) without the constant column. Returns a dict of results (see module doc)."""
    from scipy import stats

    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float).reshape(len(y), -1)
    n = len(y)
    names = list(names or [f"x{j + 1}" for j in range(X.shape[1])])
    if intercept:
        X = np.column_stack([np.ones(n), X])
        names = ["(intercept)"] + names
    k = X.shape[1]
    if n <= k:
        raise ValueError("too few observations")
    xtx_inv = np.linalg.pinv(X.T @ X)
    beta = xtx_inv @ (X.T @ y)
    fitted = X @ beta
    e = y - fitted
    rss = float(e @ e)
    df = n - k
    sigma2 = rss / df
    se = np.sqrt(np.maximum(np.diag(xtx_inv) * sigma2, 0))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = beta / se
    p = 2 * stats.t.sf(np.abs(t), df)
    # HC1 robust (White) covariance: (X'X)^-1 X' diag(e^2) X (X'X)^-1 * n / (n - k)
    meat = (X * (e ** 2)[:, None]).T @ X
    cov_r = xtx_inv @ meat @ xtx_inv * n / df
    se_r = np.sqrt(np.maximum(np.diag(cov_r), 0))
    with np.errstate(divide="ignore", invalid="ignore"):
        t_r = beta / se_r
    p_r = 2 * stats.t.sf(np.abs(t_r), df)
    tss = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - rss / tss if tss > 0 else 0.0
    adj = 1 - (1 - r2) * (n - 1) / df if intercept else r2
    # leverage -> internally studentised residuals
    h = np.einsum("ij,jk,ik->i", X, xtx_inv, X)
    with np.errstate(divide="ignore", invalid="ignore"):
        std_res = e / np.sqrt(sigma2 * np.clip(1 - h, 1e-12, None))
    # joint F test of all slopes
    q = k - 1 if intercept else k
    if q > 0 and rss > 0:
        f = ((tss - rss) / q) / (rss / df)
        f_p = float(stats.f.sf(f, q, df))
    else:
        f, f_p = math.nan, math.nan
    # Jarque-Bera
    s = e.std()
    if s > 0:
        skew = float(np.mean((e - e.mean()) ** 3) / s ** 3)
        kurt = float(np.mean((e - e.mean()) ** 4) / s ** 4)
    else:
        skew, kurt = 0.0, 3.0
    jb = n / 6.0 * (skew ** 2 + (kurt - 3) ** 2 / 4.0)
    jb_p = float(stats.chi2.sf(jb, 2))
    # Koenker-Bassett: n R^2 of e^2 regressed on X
    e2 = e ** 2
    if q > 0 and e2.std() > 0:
        g = np.linalg.lstsq(X, e2, rcond=None)[0]
        r2_aux = 1 - float(((e2 - X @ g) ** 2).sum()) / float(((e2 - e2.mean()) ** 2).sum())
        kb = n * r2_aux
        kb_p = float(stats.chi2.sf(kb, q))
    else:
        kb, kb_p = math.nan, math.nan
    return {
        "names": names, "n": n, "k": k, "df": df, "beta": beta, "se": se, "t": t, "p": p,
        "se_robust": se_r, "t_robust": t_r, "p_robust": p_r, "r2": r2, "adj_r2": adj, "rss": rss,
        "sigma2": sigma2, "aicc": aicc(rss, n, k), "fitted": fitted, "resid": e, "std_resid": std_res,
        "f": f, "f_p": f_p, "jb": jb, "jb_p": jb_p, "skew": skew, "kurtosis": kurt, "kb": kb, "kb_p": kb_p,
        "vif": vif(X[:, 1:] if intercept else X), "intercept": intercept,
    }


def vif(X):
    """Variance inflation factor of every column of X (no constant column; one is added)."""
    X = np.asarray(X, dtype=float)
    n, m = X.shape
    out = np.ones(m)
    if m < 2:
        return out
    for j in range(m):
        others = np.column_stack([np.ones(n), np.delete(X, j, axis=1)])
        xj = X[:, j]
        r = xj - others @ np.linalg.lstsq(others, xj, rcond=None)[0]
        tss = float(((xj - xj.mean()) ** 2).sum())
        r2 = 1 - float(r @ r) / tss if tss > 0 else 1.0
        out[j] = 1 / (1 - r2) if r2 < 1 - 1e-12 else math.inf
    return out


# ---------------------------------------------------------- neighbours ---

def knn_weights(coords, k=8):
    """Row-standardised k-nearest-neighbour weights (sparse; built by weights.py)."""
    from . import weights

    return weights.row_standardise(weights.knn(coords, k))


def band_weights(coords, distance):
    """Row-standardised distance-band weights; features with nobody inside the band get their nearest
    feature (both ways) so no residual is left out. Returns (W, number of such islands)."""
    from . import weights

    w = weights.distance_band(coords, distance)
    w, isl = weights.attach_islands(w, np.asarray(coords, dtype=float))
    return weights.row_standardise(w), int(len(isl))


def default_band(coords):
    """A distance band that gives every feature at least one neighbour (the largest nearest-neighbour
    distance)."""
    from . import weights

    return weights.min_threshold(coords) * 1.0001


def morans_i(values, w, permutations=999, seed=None):
    """Global Moran's I with a permutation p-value. w: row-standardised sparse matrix. Returns a dict."""
    z = np.asarray(values, dtype=float)
    z = z - z.mean()
    n = len(z)
    s0 = float(w.sum())
    zz = float(z @ z)
    if zz == 0 or s0 == 0:
        return {"I": math.nan, "expected": -1 / (n - 1), "z": math.nan, "p": math.nan, "sim": np.zeros(0)}
    i_obs = n / s0 * float(z @ (w @ z)) / zz
    sims = []
    r = rng(seed)
    left = int(permutations)
    while left > 0:  # permutations in blocks: W @ (n, block) is one sparse product per block
        b = min(left, 200)
        zp = np.stack([r.permutation(z) for _ in range(b)], axis=1)
        sims.append(n / s0 * np.sum(zp * (w @ zp), axis=0) / zz)
        left -= b
    sim = np.concatenate(sims) if sims else np.zeros(0)
    sd = sim.std() if sim.size else 0
    return {"I": i_obs, "expected": -1 / (n - 1), "z": (i_obs - sim.mean()) / sd if sd > 0 else math.nan,
            "p": perm_p(i_obs, sim) if sim.size else math.nan, "sim": sim}


# ------------------------------------------------------- reading data ---

def read_features(source, dependent, explanatory, context, feedback=None):
    """Reads a Processing feature source: one row per feature with its centroid (in metres: geographic
    layers, or layers not in metres, are projected to the local UTM zone for the distances) and the numeric values.
    Returns dict(ids, coords, y, X, ok (mask of complete rows), projected_to (authid or None), features)."""
    from . import weights
    from qgis.core import Qgis, QgsCoordinateTransform

    crs = source.sourceCrs()
    xform, projected_to = None, None
    feats = list(source.getFeatures())
    work, moved = weights.work_crs(crs, source.sourceExtent(), context)
    if moved:
        projected_to = work.authid()
        xform = QgsCoordinateTransform(crs, work, context.transformContext())
    n = len(feats)
    coords = np.full((n, 2), np.nan)
    y = np.full(n, np.nan)
    X = np.full((n, len(explanatory)), np.nan)
    ids = []
    for i, f in enumerate(feats):
        ids.append(f.id())
        g = f.geometry()
        if g is not None and not g.isNull() and not g.isEmpty():
            single_point = g.type() == Qgis.GeometryType.Point and not g.isMultipart()
            p = g.asPoint() if single_point else g.centroid().asPoint()
            if xform is not None:
                p = xform.transform(p)
            coords[i] = (p.x(), p.y())
        y[i] = _num(f[dependent])
        for j, name in enumerate(explanatory):
            X[i, j] = _num(f[name])
    ok = np.isfinite(y) & np.all(np.isfinite(X), axis=1) & np.all(np.isfinite(coords), axis=1)
    return {"ids": ids, "coords": coords, "y": y, "X": X, "ok": ok, "projected_to": projected_to,
            "features": feats}


def _num(v):
    try:
        if v is None:
            return math.nan
        if hasattr(v, "isNull") and v.isNull():
            return math.nan
        return float(v)
    except (TypeError, ValueError):
        return math.nan


# ----------------------------------------------------------- words ---

def nice_unit(x):
    """A round amount of an explanatory variable to talk about ("each extra 1,000 people")."""
    x = np.asarray(x, dtype=float)
    sd = float(np.std(x))
    if not math.isfinite(sd) or sd <= 0:
        return 1.0
    return float(10 ** round(math.log10(sd)))


def fmt(v, digits=3):
    if v is None or not math.isfinite(v):
        return "-"
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1e4:
        return f"{v:,.0f}"
    return f"{v:.{digits}g}" if a < 1 else f"{v:,.{max(0, digits - 1 - int(math.log10(a)))}f}"


def p_text(p):
    if p is None or not math.isfinite(p):
        return "p = -"
    if p < 0.001:
        return "p < 0.001"
    if p < 0.01:
        return "p < 0.01"
    return f"p = {p:.2f}"


def effect_sentences(res, X, y_name, lang=None):
    """One sentence per explanatory variable, in plain words."""
    out = []
    use_robust = res["kb_p"] < 0.05 if math.isfinite(res["kb_p"]) else False
    pv = res["p_robust"] if use_robust else res["p"]
    off = 1 if res["intercept"] else 0
    for j, name in enumerate(res["names"][off:]):
        b = float(res["beta"][j + off])
        p = float(pv[j + off])
        unit = nice_unit(X[:, j])
        eff = b * unit
        unit_s = fmt(unit, 3) if unit != 1 else "1"
        if p < 0.05:
            out.append(say({
                "ar": "كل زيادة {u} في «{x}» يصاحبها {dir} في «{y}» بنحو {e} ({p}).",
                "en": "Each extra {u} of “{x}” goes with about {e} {dir} “{y}” ({p})."},
                lang, u=unit_s, x=name, y=y_name, e=fmt(abs(eff)),
                dir=say({"ar": "زيادة" if eff > 0 else "نقص", "en": "more" if eff > 0 else "less"}, lang),
                p=p_text(p)))
        else:
            out.append(say({
                "ar": "«{x}» لا يظهر له أثر واضح على «{y}» بعد احتساب المتغيرات الأخرى ({p}).",
                "en": "“{x}” shows no clear effect on “{y}” once the other variables are counted ({p})."},
                lang, x=name, y=y_name, p=p_text(p)))
    return out


def answer_text(res, X, y_name, moran=None, lang=None):
    parts = [say({"ar": "النموذج يفسّر {r}% من التباين في «{y}» (R² المعدّل {a}).",
                  "en": "The model explains {r}% of the variation in “{y}” (adjusted R² {a})."},
                 lang, r=round(100 * res["r2"]), y=y_name, a=f"{res['adj_r2']:.2f}")]
    parts += effect_sentences(res, X, y_name, lang)
    if moran is not None and math.isfinite(moran.get("p", math.nan)):
        if moran["p"] < 0.05 and moran["I"] > 0:
            parts.append(say({
                "ar": "البواقي متجمّعة في المكان (Moran's I = {i}، {p}): غالبًا هناك متغير ناقص أو أن العلاقة "
                      "تتغير من مكان لآخر - جرّب الانحدار الموزون جغرافيًا (GWR).",
                "en": "The residuals are clustered in space (Moran's I = {i}, {p}): a variable is probably missing "
                      "or the relationships change from place to place - try geographically weighted regression "
                      "(GWR)."}, lang, i=f"{moran['I']:.3f}", p=p_text(moran["p"])))
        else:
            parts.append(say({
                "ar": "البواقي لا تظهر تجمّعًا مكانيًا واضحًا (Moran's I = {i}، {p}).",
                "en": "The residuals show no clear spatial clustering (Moran's I = {i}, {p})."},
                lang, i=f"{moran['I']:.3f}", p=p_text(moran["p"])))
    return " ".join(parts)


def warnings_list(res, lang=None):
    w = []
    off = 1 if res["intercept"] else 0
    for name, v in zip(res["names"][off:], res["vif"]):
        if v > VIF_LIMIT:
            w.append(say({"ar": "«{x}» مرتبط بقوة بالمتغيرات الأخرى (VIF = {v} > 7.5): معاملاته غير مستقرة، "
                                "احذف أحد المتغيرات المتشابهة.",
                          "en": "“{x}” is strongly tied to the other variables (VIF = {v} > 7.5): its coefficient "
                                "is unstable; drop one of the overlapping variables."}, lang, x=name, v=fmt(v)))
    if math.isfinite(res["jb_p"]) and res["jb_p"] < 0.05:
        w.append(say({"ar": "البواقي ليست طبيعية التوزيع (Jarque-Bera {p}): قد يكون هناك قيم شاذة أو علاقة غير "
                            "خطية؛ جرّب تحويل لوغاريتمي للمتغيرات الملتوية.",
                      "en": "The residuals are not normally distributed (Jarque-Bera {p}): outliers or a curved "
                            "relationship; try a log transform of skewed variables."}, lang, p=p_text(res["jb_p"])))
    if math.isfinite(res["kb_p"]) and res["kb_p"] < 0.05:
        w.append(say({"ar": "تباين البواقي غير ثابت (Koenker-Bassett {p}): استُخدمت الأخطاء المعيارية المتينة "
                            "(HC1)، وقد تكون العلاقة مختلفة من مكان لآخر.",
                      "en": "The spread of the residuals is not constant (Koenker-Bassett {p}): robust (HC1) "
                            "standard errors are used, and the relationships may differ from place to place."},
                     lang, p=p_text(res["kb_p"])))
    return w


def caveats(lang=None):
    return [
        say({"ar": "الارتباط ليس سببية: المعامل يصف كيف تتغير القيم معًا، لا أن أحدها يسبب الآخر.",
             "en": "Correlation is not causation: a coefficient says how values move together, not that one "
                   "causes the other."}, lang),
        say({"ar": "المغالطة البيئية: علاقة بين مناطق لا تنطبق بالضرورة على الأفراد داخلها.",
             "en": "Ecological fallacy: a relationship between areas does not necessarily hold for the "
                   "individuals living in them."}, lang),
        say({"ar": "تعدد الارتباط: المتغيرات المتشابهة تتقاسم الأثر فتصبح معاملاتها غير موثوقة (راجع VIF).",
             "en": "Multicollinearity: overlapping variables share the effect, so their coefficients are "
                   "unreliable (see VIF)."}, lang),
        say({"ar": "النتيجة تخص هذه البيانات وحدودها ومقياسها؛ تغيير حدود المناطق قد يغيرها (MAUP).",
             "en": "The result belongs to these data, their boundaries and scale; other zones may change it "
                   "(MAUP)."}, lang),
    ]


# ----------------------------------------------------------- charts ---

def chart_observed_predicted(y, fitted, y_name, lang=None):
    fig = figure(5.6, 4.6)
    ax = fig.add_subplot(111)
    ax.scatter(fitted, y, s=12, alpha=0.6, color="#12A594", edgecolor="none")
    lo, hi = float(min(np.min(y), np.min(fitted))), float(max(np.max(y), np.max(fitted)))
    ax.plot([lo, hi], [lo, hi], color="#555", lw=1, ls="--")
    ax.set_xlabel(chart_text(say({"ar": "القيمة المتوقعة", "en": "Predicted"}, lang)))
    ax.set_ylabel(chart_text(say({"ar": "القيمة المرصودة: {y}", "en": "Observed: {y}"}, lang, y=y_name)))
    ax.set_title(chart_text(say({"ar": "المرصود مقابل المتوقع", "en": "Observed vs predicted"}, lang)))
    ax.grid(alpha=0.3)
    return png(fig)


def chart_residual_histogram(std_resid, lang=None):
    from scipy import stats

    fig = figure(5.6, 3.8)
    ax = fig.add_subplot(111)
    r = np.asarray(std_resid, dtype=float)
    r = r[np.isfinite(r)]
    ax.hist(r, bins=min(40, max(8, int(math.sqrt(len(r))))), density=True, color="#9BD8CF", edgecolor="#12A594")
    xs = np.linspace(min(-4, r.min()), max(4, r.max()), 200)
    ax.plot(xs, stats.norm.pdf(xs), color="#C98A12", lw=1.5,
            label=chart_text(say({"ar": "التوزيع الطبيعي", "en": "Normal curve"}, lang)))
    ax.set_xlabel(chart_text(say({"ar": "البواقي المعيارية", "en": "Standardised residual"}, lang)))
    ax.set_title(chart_text(say({"ar": "توزيع البواقي", "en": "Residual histogram"}, lang)))
    ax.legend(frameon=False)
    return png(fig)


def chart_residual_map(coords, std_resid, lang=None):
    fig = figure(5.6, 5.0)
    ax = fig.add_subplot(111)
    r = np.clip(np.asarray(std_resid, dtype=float), -3, 3)
    sc = ax.scatter(coords[:, 0], coords[:, 1], c=r, cmap="RdBu_r", vmin=-3, vmax=3, s=14, edgecolor="none")
    fig.colorbar(sc, ax=ax, shrink=0.8)
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(chart_text(say({"ar": "خريطة البواقي المعيارية", "en": "Map of standardised residuals"}, lang)))
    return png(fig)
