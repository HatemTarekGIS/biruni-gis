"""Geographically weighted regression (the numbers behind biruni:ssgwr).

At every feature i a weighted least-squares regression is fitted with weights w_ij that fall with the
distance d_ij (Fotheringham, Brunsdon & Charlton 2002, "Geographically Weighted Regression"):

    beta_i = (X' W_i X)^-1 X' W_i y

Kernels: adaptive bisquare (the bandwidth is the distance to the k-th nearest neighbour, so dense and sparse
areas get the same number of neighbours), fixed Gaussian exp(-0.5 (d/h)^2) and fixed bisquare
(1 - (d/h)^2)^2 inside h. The hat matrix S has rows r_i = x_i (X'W_iX)^-1 X'W_i; its trace tr(S) is the
effective number of parameters (ENP) and

    AICc = n ln(RSS/n) + n ln(2 pi) + n (n + tr S) / (n - 2 - tr S)          (eq. 2.33)

is minimised over the bandwidth by golden-section search. Local standard errors use
sigma^2 = RSS / (n - 2 tr S + tr S'S) and Var(beta_i) = sigma^2 C_i C_i' with C_i = (X'W_iX)^-1 X'W_i.
Significance of the local t-values is judged with the Silva & Fotheringham (2016) correction for the
multiple, dependent local tests: alpha_adj = alpha * p / ENP (p = parameters per model).

Speed / complexity. With n features and p parameters (including the constant) every bandwidth evaluation is
O(n^2 p^2): the n x n weight matrix is built in row blocks and the n local cross-product matrices X'W_iX come
out of ONE matrix product, W_block (c x n) @ [x_jp x_jq] (n x p^2), done by BLAS - no Python loop over
features. The p x p systems are then solved in a batch. Memory stays O(c n + n p^2) with c ~ 2e6 / n rows per
block. A golden-section search needs ~15-25 evaluations; tr(S'S), local standard errors, local R^2 and
condition numbers are computed once, at the chosen bandwidth. 3,000 features take a few seconds.
"""
import math

import numpy as np

from .common import chart_text, figure, png, say
from .regression import aicc as aicc_formula
from .regression import fmt, ols, p_text

ADAPTIVE_BISQUARE, FIXED_GAUSSIAN, FIXED_BISQUARE = "adaptive_bisquare", "fixed_gaussian", "fixed_bisquare"
KERNEL_IDS = (ADAPTIVE_BISQUARE, FIXED_GAUSSIAN, FIXED_BISQUARE)
COND_LIMIT = 30
GOLDEN = (math.sqrt(5) - 1) / 2


class GWR:
    def __init__(self, coords, y, X, names=None, kernel=ADAPTIVE_BISQUARE):
        """coords (n,2) in metres, y (n,), X (n,m) without the constant (one is added)."""
        self.coords = np.asarray(coords, dtype=float)
        self.y = np.asarray(y, dtype=float)
        n = len(self.y)
        X = np.asarray(X, dtype=float).reshape(n, -1)
        self.X = np.column_stack([np.ones(n), X])
        self.names = ["(intercept)"] + list(names or [f"x{j + 1}" for j in range(X.shape[1])])
        self.n, self.p = self.X.shape
        if kernel not in KERNEL_IDS:
            raise ValueError(kernel)
        self.kernel = kernel
        p = self.p
        self._xx = (self.X[:, :, None] * self.X[:, None, :]).reshape(n, p * p)
        self._xy = self.X * self.y[:, None]
        self.block = max(1, min(n, int(2_000_000 // max(n, 1))))
        self.history = []  # (bandwidth, AICc) of every evaluation

    # ------------------------------------------------------------ weights
    def weights(self, rows, bw):
        from scipy.spatial.distance import cdist

        d = cdist(self.coords[rows], self.coords)
        if self.kernel == ADAPTIVE_BISQUARE:
            k = int(min(max(round(bw), 2), self.n))
            h = np.partition(d, k - 1, axis=1)[:, k - 1] * 1.0000001  # the k-th neighbour keeps a tiny weight
            h = np.where(h > 0, h, 1e-12)[:, None]
        else:
            h = float(bw)
        u = d / h
        if self.kernel == FIXED_GAUSSIAN:
            return np.exp(-0.5 * u * u)
        return np.where(u < 1, (1 - u * u) ** 2, 0.0)

    def bounds(self):
        """Search interval of the bandwidth: enough neighbours for p unknowns up to (nearly) a global model."""
        n, p = self.n, self.p
        if self.kernel == ADAPTIVE_BISQUARE:
            return float(min(n, max(p + 2, 2 * p + 2, 10))), float(n)
        from scipy.spatial import cKDTree

        kq = min(n, p + 2)
        d, _ = cKDTree(self.coords).query(self.coords, k=kq)
        d = np.atleast_2d(d)
        lo = float(np.max(d[:, -1])) * 1.01
        span = self.coords.max(axis=0) - self.coords.min(axis=0)
        diag = float(math.hypot(*span)) or 1.0
        hi = diag * (2.0 if self.kernel == FIXED_GAUSSIAN else 1.0)
        return min(lo, hi * 0.5), hi

    # --------------------------------------------------------------- fit
    @staticmethod
    def _solve(A, rhs):
        try:
            return np.linalg.solve(A, rhs)
        except np.linalg.LinAlgError:
            return np.linalg.pinv(A) @ rhs

    def _pass(self, bw, full=False):
        n, p = self.n, self.p
        beta = np.empty((n, p))
        s_ii = np.empty(n)
        out = {}
        if full:
            var_diag = np.empty((n, p))
            cond = np.empty(n)
            tr_ss = 0.0
        for start in range(0, n, self.block):
            rows = np.arange(start, min(n, start + self.block))
            w = self.weights(rows, bw)
            A = (w @ self._xx).reshape(len(rows), p, p)
            rhs = np.stack([w @ self._xy, self.X[rows]], axis=2)  # (c, p, 2): X'W y and x_i
            sol = self._solve(A, rhs)
            beta[rows] = sol[:, :, 0]
            v = sol[:, :, 1]  # (X'W_iX)^-1 x_i
            s_ii[rows] = np.sum(self.X[rows] * v, axis=1)  # w_ii = 1
            if full:
                r = (v @ self.X.T) * w  # rows of the hat matrix
                tr_ss += float(np.sum(r * r))
                Ainv = np.linalg.pinv(A)
                A2 = (w * w @ self._xx).reshape(len(rows), p, p)
                var_diag[rows] = np.einsum("cij,cjk,cik->ci", Ainv, A2, Ainv)
                dg = np.sqrt(np.clip(np.einsum("cii->ci", A), 1e-300, None))
                ev = np.linalg.eigvalsh(A / (dg[:, :, None] * dg[:, None, :]))
                cond[rows] = np.sqrt(ev[:, -1] / np.clip(ev[:, 0], 1e-300, None))
        fitted = np.sum(self.X * beta, axis=1)
        resid = self.y - fitted
        rss = float(resid @ resid)
        tr_s = float(s_ii.sum())
        out.update(beta=beta, fitted=fitted, resid=resid, rss=rss, tr_s=tr_s, s_ii=s_ii,
                   aicc=aicc_formula(rss, n, tr_s))
        if full:
            out.update(tr_ss=tr_ss, var_diag=var_diag, cond=cond)
        return out

    def aicc(self, bw):
        a = self._pass(bw)["aicc"]
        self.history.append((float(bw), a))
        return a

    def search(self, lower=None, upper=None, max_iter=60):
        """Golden-section search of the bandwidth that minimises AICc."""
        lo, hi = self.bounds()
        a = float(lower if lower is not None else lo)
        b = float(upper if upper is not None else hi)
        integer = self.kernel == ADAPTIVE_BISQUARE
        tol = 1.0 if integer else (b - a) * 1e-4
        cache = {}

        def f(x):
            x = float(round(x)) if integer else x
            if x not in cache:
                cache[x] = self.aicc(x)
            return cache[x]

        c = b - GOLDEN * (b - a)
        d = a + GOLDEN * (b - a)
        fc, fd = f(c), f(d)
        it = 0
        while abs(b - a) > tol and it < max_iter:
            it += 1
            if fc <= fd:
                b, d, fd = d, c, fc
                c = b - GOLDEN * (b - a)
                fc = f(c)
            else:
                a, c, fc = c, d, fd
                d = a + GOLDEN * (b - a)
                fd = f(d)
        f(a), f(b)  # the ends too: a nearly global model is a valid answer
        return min(cache, key=lambda x: (cache[x], -x))

    def fit(self, bw=None, alpha=0.05):
        from scipy import stats

        if bw is None:
            bw = self.search()
        r = self._pass(bw, full=True)
        n, p = self.n, self.p
        delta1 = n - 2 * r["tr_s"] + r["tr_ss"]
        sigma2 = r["rss"] / delta1 if delta1 > 0 else math.nan
        se = np.sqrt(np.maximum(r["var_diag"] * sigma2, 0))
        with np.errstate(divide="ignore", invalid="ignore"):
            t = r["beta"] / se
        enp = r["tr_s"]
        alpha_adj = alpha * p / enp if enp > 0 else alpha
        t_crit = float(stats.t.ppf(1 - alpha_adj / 2, n - 1))
        # local R^2 (Fotheringham et al.): weighted total and residual sums around each feature
        dev2 = (self.y - self.y.mean()) ** 2
        e2 = r["resid"] ** 2
        local_r2 = np.empty(n)
        for start in range(0, n, self.block):
            rows = np.arange(start, min(n, start + self.block))
            w = self.weights(rows, bw)
            tss = w @ dev2
            local_r2[rows] = np.where(tss > 0, 1 - (w @ e2) / np.where(tss > 0, tss, 1), np.nan)
        tss = float(dev2.sum())
        r2 = 1 - r["rss"] / tss if tss > 0 else 0.0
        adj_r2 = 1 - (1 - r2) * (n - 1) / (n - enp - 1) if n - enp - 1 > 0 else math.nan
        with np.errstate(divide="ignore", invalid="ignore"):
            std_res = r["resid"] / np.sqrt(sigma2 * np.clip(1 - r["s_ii"], 1e-12, None))
        r.update(bandwidth=float(bw), sigma2=sigma2, se=se, t=t, enp=enp, alpha_adj=alpha_adj, t_crit=t_crit,
                 significant=np.abs(t) >= t_crit, local_r2=local_r2, r2=r2, adj_r2=adj_r2, delta1=delta1,
                 std_resid=std_res, names=self.names, kernel=self.kernel, n=n, p=p)
        return r


def compare_with_ols(res, y, X, names=None):
    """Global OLS on the same data and the ANOVA-style comparison (Fotheringham et al. 2002, Table 2.2 / GWR3):
    is the GWR improvement larger than chance? The F test is approximate."""
    from scipy import stats

    g = ols(y, X, names)
    n, p = res["n"], res["p"]
    df_ols = n - p
    df_res = res["delta1"]
    imp_ss = g["rss"] - res["rss"]
    imp_df = df_ols - df_res
    ms_imp = imp_ss / imp_df if imp_df > 0 else math.nan
    ms_res = res["rss"] / df_res if df_res > 0 else math.nan
    f = ms_imp / ms_res if ms_res and math.isfinite(ms_imp) and ms_res > 0 else math.nan
    fp = float(stats.f.sf(f, imp_df, df_res)) if math.isfinite(f) and imp_df > 0 else math.nan
    return {"ols": g, "aicc_drop": g["aicc"] - res["aicc"],
            "anova": [("ols_resid", g["rss"], df_ols, g["rss"] / df_ols, math.nan),
                      ("gwr_improvement", imp_ss, imp_df, ms_imp, f),
                      ("gwr_resid", res["rss"], df_res, ms_res, math.nan)],
            "f": f, "f_p": fp}


# ----------------------------------------------------------------- words ---

def kernel_words(kernel, bw, lang=None):
    if kernel == ADAPTIVE_BISQUARE:
        return say({"ar": "أقرب {k} معلم حول كل معلم (bisquare متكيّف)",
                    "en": "the nearest {k} features around each feature (adaptive bisquare)"}, lang, k=int(round(bw)))
    kind = "Gaussian" if kernel == FIXED_GAUSSIAN else "bisquare"
    return say({"ar": "مسافة ثابتة {d} م ({k})", "en": "a fixed distance of {d} m ({k})"}, lang,
               d=fmt(bw), k=kind)


def answer_text(res, cmp, y_name, lang=None):
    parts = []
    drop = cmp["aicc_drop"]
    parts.append(say({"ar": "الانحدار الموزون جغرافيًا لـ«{y}» استخدم {k}، ويفسّر {r}% من التباين مقابل {o}% "
                            "للنموذج العام.",
                      "en": "Geographically weighted regression of “{y}” used {k} and explains {r}% of the "
                            "variation, against {o}% for the global model."},
                     lang, y=y_name, k=kernel_words(res["kernel"], res["bandwidth"], lang),
                     r=round(100 * res["r2"]), o=round(100 * cmp["ols"]["r2"])))
    if drop > 3:
        parts.append(say({"ar": "AICc انخفض بمقدار {d} (أكثر من 3): العلاقات تتغير فعلًا من مكان لآخر.",
                          "en": "AICc dropped by {d} (more than 3): the relationships really change from place "
                                "to place."}, lang, d=fmt(drop)))
    else:
        parts.append(say({"ar": "AICc لم ينخفض بأكثر من 3 ({d}): لا دليل كافٍ على أن العلاقات تتغير مكانيًا؛ "
                                "النموذج العام (OLS) يكفي.",
                          "en": "AICc did not drop by more than 3 ({d}): no good evidence that the relationships "
                                "vary in space; the global (OLS) model is enough."}, lang, d=fmt(drop)))
    for j, name in enumerate(res["names"][1:], start=1):
        b = res["beta"][:, j]
        sig = res["significant"][:, j]
        pos = int(np.sum(sig & (b > 0)))
        neg = int(np.sum(sig & (b < 0)))
        parts.append(say({"ar": "«{x}»: المعامل المحلي بين {lo} و{hi}؛ موجب ومعنوي في {pos} معلم، سالب ومعنوي "
                                "في {neg}، وغير معنوي في الباقي ({ns}).",
                          "en": "“{x}”: the local coefficient ranges from {lo} to {hi}; significantly positive at "
                                "{pos} features, significantly negative at {neg}, not significant at the other "
                                "{ns}."}, lang, x=name, lo=fmt(float(np.min(b))), hi=fmt(float(np.max(b))),
                         pos=pos, neg=neg, ns=res["n"] - pos - neg))
    bad = int(np.sum(res["cond"] > COND_LIMIT))
    if bad:
        parts.append(say({"ar": "في {b} معلم رقم الشرط أكبر من 30: المتغيرات متشابهة محليًا والمعاملات هناك غير "
                                "موثوقة.",
                          "en": "{b} features have a condition number above 30: the variables overlap locally and "
                                "the coefficients there are unreliable."}, lang, b=bad))
    return " ".join(parts)


def caveats(lang=None):
    return [
        say({"ar": "المعامل المحلي ليس سببًا محليًا: هو وصف لكيف تتغير القيم معًا في الجوار.",
             "en": "A local coefficient is not a local cause: it describes how values move together nearby."},
            lang),
        say({"ar": "الخرائط المحلية تتأثر بعرض النطاق؛ نطاق صغير جدًا يرسم ضجيجًا.",
             "en": "The local maps depend on the bandwidth; a very small one maps noise."}, lang),
        say({"ar": "المعاملات المخططة غير معنوية بعد تصحيح الاختبارات المتعددة (Silva & Fotheringham).",
             "en": "Hatched coefficients are not significant once the many local tests are allowed for "
                   "(Silva & Fotheringham)."}, lang),
        say({"ar": "الارتباط ليس سببية، والعلاقة بين مناطق لا تنطبق بالضرورة على الأفراد.",
             "en": "Correlation is not causation, and a relationship between areas need not hold for "
                   "individuals."}, lang),
    ]


# ----------------------------------------------------------------- charts ---

def chart_aicc(history, chosen, kernel, lang=None):
    fig = figure(5.8, 3.6)
    ax = fig.add_subplot(111)
    h = sorted((b, a) for b, a in history if math.isfinite(a))
    if h:
        ax.plot([b for b, _ in h], [a for _, a in h], "o-", color="#12A594", ms=3, lw=1)
    ax.axvline(chosen, color="#C98A12", ls="--", lw=1)
    ax.set_xlabel(chart_text(say({"ar": "عدد الجيران" if kernel == ADAPTIVE_BISQUARE else "عرض النطاق (م)",
                                  "en": "Neighbours" if kernel == ADAPTIVE_BISQUARE else "Bandwidth (m)"}, lang)))
    ax.set_ylabel("AICc")
    ax.set_title(chart_text(say({"ar": "اختيار عرض النطاق", "en": "Choosing the bandwidth"}, lang)))
    ax.grid(alpha=0.3)
    return png(fig)


def chart_coefficients(coords, res, lang=None):
    """Small maps of every local coefficient; hollow dots are not significant."""
    names = res["names"]
    m = len(names)
    cols = min(3, m)
    rows = int(math.ceil(m / cols))
    fig = figure(3.2 * cols, 3.0 * rows)
    for j, name in enumerate(names):
        ax = fig.add_subplot(rows, cols, j + 1)
        b = res["beta"][:, j]
        lim = float(np.nanmax(np.abs(b))) or 1.0
        sig = res["significant"][:, j]
        sc = ax.scatter(coords[sig, 0], coords[sig, 1], c=b[sig], cmap="RdBu_r", vmin=-lim, vmax=lim, s=10,
                        edgecolor="none")
        ax.scatter(coords[~sig, 0], coords[~sig, 1], s=10, facecolor="none", edgecolor="#9aa3ab", lw=0.5)
        fig.colorbar(sc, ax=ax, shrink=0.75)
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_title(chart_text(name), fontsize=9)
    return png(fig)


def anova_label(key, lang=None):
    return say({"ols_resid": {"ar": "بواقي OLS", "en": "OLS residuals"},
                "gwr_improvement": {"ar": "تحسّن GWR", "en": "GWR improvement"},
                "gwr_resid": {"ar": "بواقي GWR", "en": "GWR residuals"}}[key], lang)


__all__ = ["GWR", "compare_with_ols", "answer_text", "caveats", "chart_aicc", "chart_coefficients", "p_text"]
